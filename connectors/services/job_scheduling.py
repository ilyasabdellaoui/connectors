#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""
Event loop

- polls for work by calling Elasticsearch on a regular basis
- instantiates connector plugins
- mirrors an Elasticsearch index with a collection of documents
"""

import functools
from datetime import datetime, timezone

from connectors.es.client import License, with_concurrency_control
from connectors.es.index import DocumentNotFoundError
from connectors.protocol import (
    ConnectorIndex,
    DataSourceError,
    JobTriggerMethod,
    JobType,
    ServiceTypeNotConfiguredError,
    ServiceTypeNotSupportedError,
    Status,
    SyncJobIndex,
)
from connectors.services.base import BaseService
from connectors.source import get_source_klass
from connectors.utils import ConcurrentTasks


class JobSchedulingService(BaseService):
    name = "schedule"

    def __init__(self, config):
        super().__init__(config, "job_scheduling_service")
        self.idling = self.service_config["idling"]
        self.heartbeat_interval = self.service_config["heartbeat"]
        self.source_list = config["sources"]
        self.first_run = True
        self.last_wake_up_time = datetime.now(timezone.utc)
        self.max_concurrency = self.service_config.get(
            "max_concurrent_scheduling_tasks"
        )
        self.schedule_tasks_pool = ConcurrentTasks(max_concurrency=self.max_concurrency)

    def stop(self):
        super().stop()
        self.schedule_tasks_pool.cancel()

    async def _schedule(self, connector):
        # To do some first-time stuff
        just_started = self.first_run
        self.first_run = False

        if self.running is False:
            connector.log_debug("Skipping run because service is terminating")
            return

        if connector.native:
            connector.log_debug("Natively supported")

        try:
            await connector.prepare(
                self.connectors.get(connector.id, {}), self.config["sources"]
            )
        except DocumentNotFoundError:
            connector.log_error("Couldn't find connector")
            return
        except ServiceTypeNotConfiguredError:
            connector.log_error("Service type is not configured")
            return
        except ServiceTypeNotSupportedError:
            connector.log_debug(f"Can't handle source of type {connector.service_type}")
            return
        except DataSourceError as e:
            await connector.error(e)
            connector.log_error(e, exc_info=True)
            raise

        # the heartbeat is always triggered
        await connector.heartbeat(self.heartbeat_interval, force=just_started)

        connector.log_debug(f"Status is {connector.status}")

        # we trigger a sync
        if connector.status == Status.CREATED:
            connector.log_info(
                "Connector has just been created and cannot sync. Wait for Kibana to initialise connector correctly before proceeding."
            )
            return

        if connector.status == Status.NEEDS_CONFIGURATION:
            connector.log_info(
                "Connector is not configured yet. Finish connector configuration in Kibana to make it possible to run a sync."
            )
            return

        if connector.service_type not in self.source_list:
            msg = f"Couldn't find data source class for {connector.service_type}"
            raise DataSourceError(msg)

        source_klass = get_source_klass(self.source_list[connector.service_type])
        data_source = source_klass(connector.configuration)
        data_source.set_logger(connector.logger)

        try:
            connector.log_debug("Validating configuration")
            data_source.validate_config_fields()
            await data_source.validate_config()

            connector.log_debug("Pinging the backend")
            await data_source.ping()

            if connector.features.sync_rules_enabled():
                await connector.validate_filtering(validator=data_source)

            connector.log_debug(
                "Connector is configured correctly and can reach the data source"
            )
            await connector.connected()
        except Exception as e:
            connector.log_error(e, exc_info=True)
            await connector.error(e)
            return
        finally:
            await data_source.close()

        if connector.features.document_level_security_enabled():
            (
                is_platinum_license_enabled,
                license_enabled,
            ) = await self.connector_index.has_active_license_enabled(License.PLATINUM)  # pyright: ignore

            if is_platinum_license_enabled:
                await self._try_schedule_sync(connector, JobType.ACCESS_CONTROL)
            else:
                connector.log_error(
                    f"Minimum required Elasticsearch license: '{License.PLATINUM.value}'. Actual license: '{license_enabled.value}'. Skipping access control sync scheduling..."
                )

        if (
            connector.features.incremental_sync_enabled()
            and source_klass.incremental_sync_enabled
        ):
            await self._try_schedule_sync(connector, JobType.INCREMENTAL)

        await self._try_schedule_sync(connector, JobType.FULL)

    async def _run(self):
        """Main event loop."""
        self.connector_index = ConnectorIndex(self.es_config)
        self.sync_job_index = SyncJobIndex(self.es_config)

        native_service_types = self.config.get("native_service_types", []) or []
        if len(native_service_types) > 0:
            self.logger.debug(
                f"Native support for job scheduling for {', '.join(native_service_types)}"
            )
        else:
            self.logger.debug("No native service types configured for job scheduling")
        connector_ids = list(self.connectors.keys())

        self.logger.info(
            f"Job Scheduling Service started, listening to events from {self.es_config['host']}"
        )

        try:
            while self.running:
                try:
                    self.logger.debug(
                        f"Polling every {self.idling} seconds for Job Scheduling"
                    )
                    async for connector in self.connector_index.supported_connectors(
                        native_service_types=native_service_types,
                        connector_ids=connector_ids,
                    ):
                        if not self.schedule_tasks_pool.try_put(
                            functools.partial(self._schedule, connector)
                        ):
                            connector.log_debug(
                                f"Job Scheduling service is already running {self.max_concurrency} concurrent scheduling jobs and can't run more at this point. Increase 'max_concurrent_scheduling_tasks' in config if you want the service to run more concurrent scheduling jobs."  # pyright: ignore
                            )

                except Exception as e:
                    self.logger.error(e, exc_info=True)
                    self.raise_if_spurious(e)

                # Immediately break instead of sleeping
                await self.schedule_tasks_pool.join()
                if not self.running:
                    break
                self.last_wake_up_time = datetime.now(timezone.utc)
                await self._sleeps.sleep(self.idling)
        finally:
            if self.connector_index is not None:
                self.connector_index.stop_waiting()
                await self.connector_index.close()
            if self.sync_job_index is not None:
                self.sync_job_index.stop_waiting()
                await self.sync_job_index.close()
        return 0

    async def _try_schedule_sync(self, connector, job_type):
        this_wake_up_time = datetime.now(timezone.utc)
        last_wake_up_time = self.last_wake_up_time

        self.logger.debug(
            f"Scheduler woke up at {this_wake_up_time}. Previously woke up at {last_wake_up_time}."
        )

        @with_concurrency_control()
        async def _should_schedule(job_type):
            try:
                await connector.reload()
            except DocumentNotFoundError:
                connector.log_error("Couldn't reload connector")
                return False

            job_type_value = job_type.value

            last_sync_scheduled_at = connector.last_sync_scheduled_at_by_job_type(
                job_type
            )

            self.logger.debug(f"Last sync was scheduled at {last_sync_scheduled_at}")

            if (
                last_sync_scheduled_at is not None
                and last_sync_scheduled_at > last_wake_up_time
            ):
                connector.log_debug(
                    f"A scheduled '{job_type_value}' sync is created by another connector instance, skipping..."
                )
                return False

            try:
                next_sync = connector.next_sync(job_type, last_wake_up_time)
                connector.log_debug(f"Next '{job_type_value}' sync is at {next_sync}")
            except Exception as e:
                connector.log_error(e, exc_info=True)
                await connector.error(str(e))
                return False

            if next_sync is None:
                connector.log_debug(f"'{job_type_value}' sync scheduling is disabled")
                return False

            if this_wake_up_time < next_sync:
                next_sync_due = (next_sync - datetime.now(timezone.utc)).total_seconds()
                connector.log_debug(
                    f"Next '{job_type_value}' sync due in {int(next_sync_due)} seconds"
                )
                return False

            await connector.update_last_sync_scheduled_at_by_job_type(
                job_type, next_sync
            )

            return True

        if await _should_schedule(job_type):
            connector.log_info(f"Creating a scheduled '{job_type.value}' sync...")
            await self.sync_job_index.create(
                connector=connector,
                trigger_method=JobTriggerMethod.SCHEDULED,
                job_type=job_type,
            )
