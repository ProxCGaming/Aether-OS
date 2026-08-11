"""Windows Job Object wrapper to enforce resource limits on worker processes."""

import logging
import time

try:
    import win32api
    import win32con
    import win32job
except ImportError:
    win32api = None
    win32con = None
    win32job = None

logger = logging.getLogger("aether_engine.workers.job_object")

class WorkerJobObject:
    def __init__(self, time_cap_seconds: int, memory_cap_mb: int):
        """Initialize a Windows Job Object with time and memory caps."""
        if not win32job:
            raise RuntimeError("pywin32 is not installed or available.")
            
        self.job = win32job.CreateJobObject(None, "")
        
        # Configure basic limits
        basic_limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicLimitInformation)
        
        if time_cap_seconds > 0:
            basic_limits['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_JOB_TIME
            # PerJobUserTimeLimit expects 100-nanosecond ticks
            basic_limits['PerJobUserTimeLimit'] = int(time_cap_seconds * 10_000_000)
            
        win32job.SetInformationJobObject(self.job, win32job.JobObjectBasicLimitInformation, basic_limits)
        
        # Configure extended limits for memory
        if memory_cap_mb > 0:
            extended_limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            extended_limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            extended_limits['JobMemoryLimit'] = int(memory_cap_mb * 1024 * 1024)
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, extended_limits)
            
    def assign_process(self, pid: int):
        """Assign an existing process to the Job Object."""
        try:
            # PROCESS_SET_QUOTA (0x0100) and PROCESS_TERMINATE (0x0001) required
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            h_process = win32api.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            win32job.AssignProcessToJobObject(self.job, h_process)
            win32api.CloseHandle(h_process)
            logger.info(f"Assigned PID {pid} to Job Object")
        except Exception as e:
            logger.error(f"Failed to assign process {pid} to Job Object: {e}")
            raise
