"""The Scale Mesos scheduler"""
from __future__ import unicode_literals

import datetime
import logging
import threading

from django.db import DatabaseError
from django.utils.timezone import now

from error.models import Error
from job.execution.running.job_exe import RunningJobExecution
from job.execution.running.manager import RunningJobExecutionManager
from job.execution.running.tasks.results import TaskResults
from job.models import JobExecution
from job.resources import NodeResources
from mesos_api import utils
from mesos_api.api import get_slave_task_directory, get_slave_task_url, get_slave_task_file
from queue.models import Queue
from scheduler.initialize import initialize_system
from scheduler.models import Scheduler
from scheduler.offer.manager import OfferManager
from scheduler.offer.offer import ResourceOffer
from scheduler.sync.job_type_manager import JobTypeManager
from scheduler.sync.node_manager import NodeManager
from scheduler.sync.scheduler_manager import SchedulerManager
from scheduler.threads.db_sync import DatabaseSyncThread
from scheduler.threads.recon import ReconciliationThread
from scheduler.threads.schedule import SchedulingThread


logger = logging.getLogger(__name__)


try:
    from mesos.interface import Scheduler as MesosScheduler
    from mesos.interface import mesos_pb2
    logger.info('Successfully imported native Mesos bindings')
except ImportError:
    logger.info('No native Mesos bindings, falling back to stubs')
    from mesos_api.mesos import Scheduler as MesosScheduler
    import mesos_api.mesos_pb2 as mesos_pb2


class ScaleScheduler(MesosScheduler):
    """Mesos scheduler for the Scale framework"""

    # Warning threshold for normal callbacks (those with no external calls, e.g. database queries)
    NORMAL_WARN_THRESHOLD = datetime.timedelta(milliseconds=5)

    # Warning threshold for callbacks that include database queries
    DATABASE_WARN_THRESHOLD = datetime.timedelta(milliseconds=100)

    def __init__(self, executor):
        """Constructor

        :param executor: The executor to use for launching tasks
        :type executor: :class:`mesos_pb2.ExecutorInfo`
        """

        self._driver = None
        self._executor = executor
        self._framework_id = None
        self._master_hostname = None
        self._master_port = None

        self._job_exe_manager = RunningJobExecutionManager()
        self._job_type_manager = JobTypeManager()
        self._node_manager = NodeManager()
        self._offer_manager = OfferManager()
        self._scheduler_manager = SchedulerManager()

        self._db_sync_thread = None
        self._recon_thread = None
        self._scheduling_thread = None

    def registered(self, driver, frameworkId, masterInfo):
        '''
        Invoked when the scheduler successfully registers with a Mesos master.
        It is called with the frameworkId, a unique ID generated by the
        master, and the masterInfo which is information about the master
        itself.

        See documentation for :meth:`mesos_api.mesos.Scheduler.registered`.
        '''

        self._driver = driver
        self._framework_id = frameworkId.value
        self._master_hostname = masterInfo.hostname
        self._master_port = masterInfo.port
        Scheduler.objects.update_master(self._master_hostname, self._master_port)
        logger.info('Scale scheduler registered as framework %s with Mesos master at %s:%i',
                    self._framework_id, self._master_hostname, self._master_port)

        initialize_system()

        # Initial database sync
        self._job_type_manager.sync_with_database()
        self._scheduler_manager.sync_with_database()

        # Start up background threads
        self._db_sync_thread = DatabaseSyncThread(self._driver, self._job_exe_manager, self._job_type_manager,
                                                  self._node_manager, self._scheduler_manager)
        db_sync_thread = threading.Thread(target=self._db_sync_thread.run)
        db_sync_thread.daemon = True
        db_sync_thread.start()

        self._recon_thread = ReconciliationThread(self._driver)
        recon_thread = threading.Thread(target=self._recon_thread.run)
        recon_thread.daemon = True
        recon_thread.start()

        self._scheduling_thread = SchedulingThread(self._driver, self._job_exe_manager, self._job_type_manager,
                                                   self._node_manager, self._offer_manager, self._scheduler_manager)
        scheduling_thread = threading.Thread(target=self._scheduling_thread.run)
        scheduling_thread.daemon = True
        scheduling_thread.start()

        self._reconcile_running_jobs()

    def reregistered(self, driver, masterInfo):
        '''
        Invoked when the scheduler re-registers with a newly elected Mesos
        master.  This is only called when the scheduler has previously been
        registered.  masterInfo contains information about the newly elected
        master.

        See documentation for :meth:`mesos_api.mesos.Scheduler.reregistered`.
        '''

        self._driver = driver
        self._master_hostname = masterInfo.hostname
        self._master_port = masterInfo.port
        Scheduler.objects.update_master(self._master_hostname, self._master_port)
        logger.info('Scale scheduler re-registered with Mesos master at %s:%i',
                    self._master_hostname, self._master_port)

        # Update driver for background threads
        self._db_sync_thread.driver = self._driver
        self._recon_thread.driver = self._driver
        self._scheduling_thread.driver = self._driver

        self._reconcile_running_jobs()

    def disconnected(self, driver):
        '''
        Invoked when the scheduler becomes disconnected from the master, e.g.
        the master fails and another is taking over.

        See documentation for :meth:`mesos_api.mesos.Scheduler.disconnected`.
        '''

        if self._master_hostname:
            logger.error('Scale scheduler disconnected from the Mesos master at %s:%i',
                         self._master_hostname, self._master_port)
        else:
            logger.error('Scale scheduler disconnected from the Mesos master')

    def resourceOffers(self, driver, offers):
        '''
        Invoked when resources have been offered to this framework. A single
        offer will only contain resources from a single slave.  Resources
        associated with an offer will not be re-offered to _this_ framework
        until either (a) this framework has rejected those resources (see
        SchedulerDriver.launchTasks) or (b) those resources have been
        rescinded (see Scheduler.offerRescinded).  Note that resources may be
        concurrently offered to more than one framework at a time (depending
        on the allocator being used).  In that case, the first framework to
        launch tasks using those resources will be able to use them while the
        other frameworks will have those resources rescinded (or if a
        framework has already launched tasks with those resources then those
        tasks will fail with a TASK_LOST status and a message saying as much).

        See documentation for :meth:`mesos_api.mesos.Scheduler.resourceOffers`.
        '''

        started = now()

        agent_ids = []
        resource_offers = []
        for offer in offers:
            offer_id = offer.id.value
            agent_id = offer.slave_id.value
            disk = 0
            mem = 0
            cpus = 0
            for resource in offer.resources:
                if resource.name == 'disk':
                    disk = resource.scalar.value
                elif resource.name == 'mem':
                    mem = resource.scalar.value
                elif resource.name == 'cpus':
                    cpus = resource.scalar.value
            resources = NodeResources(cpus=cpus, mem=mem, disk=disk)
            agent_ids.append(agent_id)
            resource_offers.append(ResourceOffer(offer_id, agent_id, resources))

        self._node_manager.add_agent_ids(agent_ids)
        self._offer_manager.add_new_offers(resource_offers)

        duration = now() - started
        msg = 'Scheduler resourceOffers() took %.3f seconds'
        if duration > ScaleScheduler.NORMAL_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def offerRescinded(self, driver, offerId):
        '''
        Invoked when an offer is no longer valid (e.g., the slave was lost or
        another framework used resources in the offer.) If for whatever reason
        an offer is never rescinded (e.g., dropped message, failing over
        framework, etc.), a framwork that attempts to launch tasks using an
        invalid offer will receive TASK_LOST status updats for those tasks.

        See documentation for :meth:`mesos_api.mesos.Scheduler.offerRescinded`.
        '''

        started = now()

        offer_id = offerId.value
        self._offer_manager.remove_offers([offer_id])

        duration = now() - started
        msg = 'Scheduler offerRescinded() took %.3f seconds'
        if duration > ScaleScheduler.NORMAL_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def statusUpdate(self, driver, status):
        '''
        Invoked when the status of a task has changed (e.g., a slave is lost
        and so the task is lost, a task finishes and an executor sends a
        status update saying so, etc.) Note that returning from this callback
        acknowledges receipt of this status update.  If for whatever reason
        the scheduler aborts during this callback (or the process exits)
        another status update will be delivered.  Note, however, that this is
        currently not true if the slave sending the status update is lost or
        fails during that time.

        See documentation for :meth:`mesos_api.mesos.Scheduler.statusUpdate`.
        '''

        started = now()

        task_id = status.task_id.value
        job_exe_id = RunningJobExecution.get_job_exe_id(task_id)
        logger.info('Status update for task %s: %s', task_id, utils.status_to_string(status.state))

        # Since we have a status update for this task, remove it from reconciliation set
        self._recon_thread.remove_task_id(task_id)

        try:
            running_job_exe = self._job_exe_manager.get_job_exe(job_exe_id)

            if running_job_exe:
                results = TaskResults(task_id)
                results.exit_code = utils.parse_exit_code(status)
                results.when = utils.get_status_timestamp(status)
                if status.state != mesos_pb2.TASK_LOST:
                    try:
                        log_start_time = now()
                        hostname = running_job_exe._node_hostname
                        port = running_job_exe._node_port
                        task_dir = get_slave_task_directory(hostname, port, task_id)
                        results.stdout = get_slave_task_file(hostname, port, task_dir, 'stdout')
                        results.stderr = get_slave_task_file(hostname, port, task_dir, 'stderr')
                        log_end_time = now()
                        logger.debug('Time to pull logs for task: %s', str(log_end_time - log_start_time))
                    except Exception:
                        logger.exception('Error pulling logs for task %s', task_id)

                # Apply status update to running job execution
                if status.state == mesos_pb2.TASK_RUNNING:
                    hostname = running_job_exe._node_hostname
                    port = running_job_exe._node_port
                    task_dir = get_slave_task_directory(hostname, port, task_id)
                    stdout_url = get_slave_task_url(hostname, port, task_dir, 'stdout')
                    stderr_url = get_slave_task_url(hostname, port, task_dir, 'stderr')
                    running_job_exe.task_running(task_id, results.when, stdout_url, stderr_url)
                elif status.state == mesos_pb2.TASK_FINISHED:
                    running_job_exe.task_complete(results)
                elif status.state == mesos_pb2.TASK_LOST:
                    running_job_exe.task_fail(results, Error.objects.get_builtin_error('mesos-lost'))
                elif status.state in [mesos_pb2.TASK_ERROR, mesos_pb2.TASK_FAILED, mesos_pb2.TASK_KILLED]:
                    running_job_exe.task_fail(results)

                # Remove finished job execution
                if running_job_exe.is_finished():
                    self._job_exe_manager.remove_job_exe(job_exe_id)
            else:
                # Scheduler doesn't have any knowledge of this job execution
                Queue.objects.handle_job_failure(job_exe_id, now(), Error.objects.get_builtin_error('scheduler-lost'))
        except Exception:
            logger.exception('Error handling status update for job execution: %s', job_exe_id)
            # Error handling status update, add task so it can be reconciled
            self._recon_thread.add_task_ids([task_id])

        duration = now() - started
        msg = 'Scheduler statusUpdate() took %.3f seconds'
        if duration > ScaleScheduler.DATABASE_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def frameworkMessage(self, driver, executorId, slaveId, message):
        '''
        Invoked when an executor sends a message. These messages are best
        effort; do not expect a framework message to be retransmitted in any
        reliable fashion.

        See documentation for :meth:`mesos_api.mesos.Scheduler.frameworkMessage`.
        '''

        started = now()

        agent_id = slaveId.value
        node = self._node_manager.get_node(agent_id)

        if node:
            logger.info('Message from %s on host %s: %s', executorId.value, node.hostname, message)
        else:
            logger.info('Message from %s on agent %s: %s', executorId.value, agent_id, message)

        duration = now() - started
        msg = 'Scheduler frameworkMessage() took %.3f seconds'
        if duration > ScaleScheduler.NORMAL_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def slaveLost(self, driver, slaveId):
        '''
        Invoked when a slave has been determined unreachable (e.g., machine
        failure, network partition.) Most frameworks will need to reschedule
        any tasks launched on this slave on a new slave.

        See documentation for :meth:`mesos_api.mesos.Scheduler.slaveLost`.
        '''

        started = now()

        agent_id = slaveId.value
        node = self._node_manager.get_node(agent_id)

        if node:
            logger.error('Node lost on host %s', node.hostname)
        else:
            logger.error('Node lost on agent %s', agent_id)

        self._node_manager.lost_node(agent_id)
        self._offer_manager.lost_node(agent_id)

        # Fail job executions that were running on the lost node
        if node:
            for running_job_exe in self._job_exe_manager.get_job_exes_on_node(node.id):
                try:
                    running_job_exe.execution_lost(started)
                except DatabaseError:
                    logger.exception('Error failing lost job execution: %s', running_job_exe.id)
                    # Error failing execution, add task so it can be reconciled
                    task = running_job_exe.current_task
                    if task:
                        self._recon_thread.add_task_ids([task.id])
                if running_job_exe.is_finished():
                    self._job_exe_manager.remove_job_exe(running_job_exe.id)

        duration = now() - started
        msg = 'Scheduler slaveLost() took %.3f seconds'
        if duration > ScaleScheduler.DATABASE_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def executorLost(self, driver, executorId, slaveId, status):
        '''
        Invoked when an executor has exited/terminated. Note that any tasks
        running will have TASK_LOST status updates automatically generated.

        See documentation for :meth:`mesos_api.mesos.Scheduler.executorLost`.
        '''

        started = now()

        agent_id = slaveId.value
        node = self._node_manager.get_node(agent_id)

        if node:
            logger.error('Executor %s lost on host: %s', executorId.value, node.hostname)
        else:
            logger.error('Executor %s lost on agent: %s', executorId.value, agent_id)

        duration = now() - started
        msg = 'Scheduler executorLost() took %.3f seconds'
        if duration > ScaleScheduler.NORMAL_WARN_THRESHOLD:
            logger.warning(msg, duration.total_seconds())
        else:
            logger.debug(msg, duration.total_seconds())

    def error(self, driver, message):
        '''
        Invoked when there is an unrecoverable error in the scheduler or
        scheduler driver.  The driver will be aborted BEFORE invoking this
        callback.

        See documentation for :meth:`mesos_api.mesos.Scheduler.error`.
        '''

        logger.error('Unrecoverable error: %s', message)

    def shutdown(self):
        '''Performs any clean up required by this scheduler implementation.

        Currently this method just notifies any background threads to break out of their work loops.
        '''

        logger.info('Scheduler shutdown invoked, stopping background threads')
        self._db_sync_thread.shutdown()
        self._recon_thread.shutdown()
        self._scheduling_thread.shutdown()

    def _reconcile_running_jobs(self):
        """Looks up all currently running jobs in the database and sets them up to be reconciled with Mesos"""

        # List of task IDs to reconcile
        task_ids = []

        # Query for job executions that are running
        job_exes = JobExecution.objects.get_running_job_exes()

        # Find current task IDs for running executions
        for job_exe in job_exes:
            running_job_exe = self._job_exe_manager.get_job_exe(job_exe.id)
            if running_job_exe:
                task = running_job_exe.current_task
                if task:
                    task_ids.append(task.id)
            else:
                # Fail any executions that the scheduler has lost
                Queue.objects.handle_job_failure(job_exe.id, now(), Error.objects.get_builtin_error('scheduler-lost'))

        # Send task IDs to reconciliation thread
        self._recon_thread.add_task_ids(task_ids)
