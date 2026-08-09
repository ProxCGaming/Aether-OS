import pytest

def test_job_object_creation_and_limits():
    try:
        import win32job
        from aether_engine.workers.job_object import WorkerJobObject
    except ImportError:
        pytest.skip("pywin32 not installed or not on Windows")

    job = WorkerJobObject(time_cap_seconds=10, memory_cap_mb=500)
    
    basic_limits = win32job.QueryInformationJobObject(job.job, win32job.JobObjectBasicLimitInformation)
    assert basic_limits['LimitFlags'] & win32job.JOB_OBJECT_LIMIT_JOB_TIME
    assert basic_limits['PerJobUserTimeLimit'] == 10 * 10_000_000

    extended_limits = win32job.QueryInformationJobObject(job.job, win32job.JobObjectExtendedLimitInformation)
    assert extended_limits['BasicLimitInformation']['LimitFlags'] & win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
    assert extended_limits['JobMemoryLimit'] == 500 * 1024 * 1024
