"""
Thread management utilities for cyber data collection.
"""

import concurrent.futures
import logging
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class ThreadManager:
    """Manages thread pools for data collection tasks."""

    def __init__(self, max_threads: int = 4):
        """
        Initialize thread manager.

        Args:
            max_threads: Maximum number of threads to use
        """
        self.max_threads = max_threads
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

    def __enter__(self):
        """Enter context manager."""
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None

    def submit_task(self, func: Callable, *args, **kwargs) -> concurrent.futures.Future:
        """
        Submit a task to the thread pool.

        Args:
            func: Function to execute
            *args: Arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Future object for the task
        """
        if not self._executor:
            raise RuntimeError("ThreadManager not initialized. Use with context manager.")

        return self._executor.submit(func, *args, **kwargs)

    def execute_tasks(self, tasks: List[tuple]) -> List[Any]:
        """
        Execute multiple tasks and return results.

        Args:
            tasks: List of tuples (func, args, kwargs)

        Returns:
            List of results in the same order as tasks
        """
        if not self._executor:
            raise RuntimeError("ThreadManager not initialized. Use with context manager.")

        # Submit all tasks
        futures = []
        for task in tasks:
            func = task[0]
            args = task[1] if len(task) > 1 else ()
            kwargs = task[2] if len(task) > 2 else {}
            future = self._executor.submit(func, *args, **kwargs)
            futures.append(future)

        # Collect results
        results = []
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"Task execution failed: {e}")
                results.append(None)

        return results

    @property
    def is_active(self) -> bool:
        """Check if thread manager is active."""
        return self._executor is not None