import logging
import time
import math

from parsl.executors import IPyParallelExecutor, HighThroughputExecutor, ExtremeScaleExecutor


logger = logging.getLogger(__name__)


class Strategy(object):
    """FlowControl strategy.

    As a workflow dag is processed by Parsl, new tasks are added and completed
    asynchronously. Parsl interfaces executors with execution providers to construct
    scalable executors to handle the variable work-load generated by the
    workflow. This component is responsible for periodically checking outstanding
    tasks and available compute capacity and trigger scaling events to match
    workflow needs.

    Here's a diagram of an executor. An executor consists of blocks, which are usually
    created by single requests to a Local Resource Manager (LRM) such as slurm,
    condor, torque, or even AWS API. The blocks could contain several task blocks
    which are separate instances on workers.


    .. code:: python

                |<--min_blocks     |<-init_blocks              max_blocks-->|
                +----------------------------------------------------------+
                |  +--------block----------+       +--------block--------+ |
     executor = |  | task          task    | ...   |    task      task   | |
                |  +-----------------------+       +---------------------+ |
                +----------------------------------------------------------+

    The relevant specification options are:
       1. min_blocks: Minimum number of blocks to maintain
       2. init_blocks: number of blocks to provision at initialization of workflow
       3. max_blocks: Maximum number of blocks that can be active due to one workflow


    .. code:: python

          slots = current_capacity * tasks_per_node * nodes_per_block

          active_tasks = pending_tasks + running_tasks

          Parallelism = slots / tasks
                      = [0, 1] (i.e,  0 <= p <= 1)

    For example:

    When p = 0,
         => compute with the least resources possible.
         infinite tasks are stacked per slot.

         .. code:: python

               blocks =  min_blocks           { if active_tasks = 0
                         max(min_blocks, 1)   {  else

    When p = 1,
         => compute with the most resources.
         one task is stacked per slot.

         .. code:: python

               blocks = min ( max_blocks,
                        ceil( active_tasks / slots ) )


    When p = 1/2,
         => We stack upto 2 tasks per slot before we overflow
         and request a new block


    let's say min:init:max = 0:0:4 and task_blocks=2
    Consider the following example:
    min_blocks = 0
    init_blocks = 0
    max_blocks = 4
    tasks_per_node = 2
    nodes_per_block = 1

    In the diagram, X <- task

    at 2 tasks:

    .. code:: python

        +---Block---|
        |           |
        | X      X  |
        |slot   slot|
        +-----------+

    at 5 tasks, we overflow as the capacity of a single block is fully used.

    .. code:: python

        +---Block---|       +---Block---|
        | X      X  | ----> |           |
        | X      X  |       | X         |
        |slot   slot|       |slot   slot|
        +-----------+       +-----------+

    """

    def __init__(self, dfk):
        """Initialize strategy."""
        self.dfk = dfk
        self.config = dfk.config
        self.executors = {}
        self.max_idletime = 60 * 2  # 2 minutes

        for e in self.dfk.config.executors:
            self.executors[e.label] = {'idle_since': None, 'config': e.label}

        self.strategies = {None: self._strategy_noop, 'simple': self._strategy_simple}

        self.strategize = self.strategies[self.config.strategy]
        self.logger_flag = False
        self.prior_loghandlers = set(logging.getLogger().handlers)

        logger.debug("Scaling strategy: {0}".format(self.config.strategy))

    def _strategy_noop(self, tasks, *args, kind=None, **kwargs):
        """Do nothing.

        Args:
            - tasks (task_ids): Not used here.

        KWargs:
            - kind (Not used)
        """

    def unset_logging(self):
        """ Mute newly added handlers to the root level, right after calling executor.status
        """
        if self.logger_flag is True:
            return

        root_logger = logging.getLogger()

        for hndlr in root_logger.handlers:
            if hndlr not in self.prior_loghandlers:
                hndlr.setLevel(logging.ERROR)

        self.logger_flag = True

    def _strategy_simple(self, tasks, *args, kind=None, **kwargs):
        """Peek at the DFK and the executors specified.

        We assume here that tasks are not held in a runnable
        state, and that all tasks from an app would be sent to
        a single specific executor, i.e tasks cannot be specified
        to go to one of more executors.

        Args:
            - tasks (task_ids): Not used here.

        KWargs:
            - kind (Not used)
        """

        for label, executor in self.dfk.executors.items():
            if not executor.scaling_enabled:
                continue

            # Tasks that are either pending completion
            active_tasks = executor.outstanding

            status = executor.status()
            self.unset_logging()

            # FIXME we need to handle case where provider does not define these
            # FIXME probably more of this logic should be moved to the provider
            min_blocks = executor.provider.min_blocks
            max_blocks = executor.provider.max_blocks
            if isinstance(executor, IPyParallelExecutor):
                tasks_per_node = executor.workers_per_node
            elif isinstance(executor, HighThroughputExecutor):
                # This is probably wrong calculation, we need this to come from the executor
                # since we can't know slots ahead of time.
                tasks_per_node = 1
            elif isinstance(executor, ExtremeScaleExecutor):
                tasks_per_node = executor.ranks_per_node

            nodes_per_block = executor.provider.nodes_per_block
            parallelism = executor.provider.parallelism

            running = sum([1 for x in status if x == 'RUNNING'])
            submitting = sum([1 for x in status if x == 'SUBMITTING'])
            pending = sum([1 for x in status if x == 'PENDING'])
            active_blocks = running + submitting + pending
            active_slots = active_blocks * tasks_per_node * nodes_per_block

            if (isinstance(executor, IPyParallelExecutor) or
                isinstance(executor, HighThroughputExecutor) or
                isinstance(executor, ExtremeScaleExecutor)):
                logger.debug('Executor {} has {} active tasks, {}/{}/{} running/submitted/pending blocks, and {} connected engines'.format(
                    label, active_tasks, running, submitting, pending, len(executor.connected_workers)))
            else:
                logger.debug('Executor {} has {} active tasks and {}/{}/{} running/submitted/pending blocks'.format(
                    label, active_tasks, running, submitting, pending))

            # reset kill timer if executor has active tasks
            if active_tasks > 0 and self.executors[executor.label]['idle_since']:
                self.executors[executor.label]['idle_since'] = None

            # Case 1
            # No tasks.
            if active_tasks == 0:
                # Case 1a
                # Fewer blocks that min_blocks
                if active_blocks <= min_blocks:
                    # Ignore
                    # logger.debug("Strategy: Case.1a")
                    pass

                # Case 1b
                # More blocks than min_blocks. Scale down
                else:
                    # We want to make sure that max_idletime is reached
                    # before killing off resources
                    if not self.executors[executor.label]['idle_since']:
                        logger.debug("Executor {} has 0 active tasks; starting kill timer (if idle time exceeds {}s, resources will be removed)".format(
                            label, self.max_idletime)
                        )
                        self.executors[executor.label]['idle_since'] = time.time()

                    idle_since = self.executors[executor.label]['idle_since']
                    if (time.time() - idle_since) > self.max_idletime:
                        # We have resources idle for the max duration,
                        # we have to scale_in now.
                        logger.debug("Idle time has reached {}s for executor {}; removing resources".format(
                            self.max_idletime, label)
                        )
                        executor.scale_in(active_blocks - min_blocks)

                    else:
                        pass
                        # logger.debug("Strategy: Case.1b. Waiting for timer : {0}".format(idle_since))

            # Case 2
            # More tasks than the available slots.
            elif (float(active_slots) / active_tasks) < parallelism:
                # Case 2a
                # We have the max blocks possible
                if active_blocks >= max_blocks:
                    # Ignore since we already have the max nodes
                    # logger.debug("Strategy: Case.2a")
                    pass

                # Case 2b
                else:
                    # logger.debug("Strategy: Case.2b")
                    excess = math.ceil((active_tasks * parallelism) - active_slots)
                    excess_blocks = math.ceil(float(excess) / (tasks_per_node * nodes_per_block))
                    logger.debug("Requesting {} more blocks".format(excess_blocks))
                    executor.scale_out(excess_blocks)

            elif active_slots == 0 and active_tasks > 0:
                # Case 4
                # Check if slots are being lost quickly ?
                logger.debug("Requesting single slot")
                executor.scale_out(1)
            # Case 3
            # tasks ~ slots
            else:
                # logger.debug("Strategy: Case 3")
                pass


if __name__ == '__main__':

    pass
