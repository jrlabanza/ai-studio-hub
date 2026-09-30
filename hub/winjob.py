"""Windows job object: every process the hub starts dies with the hub.

Closing the hub window must never leave four GPU servers running invisibly. A job object with
KILL_ON_JOB_CLOSE makes Windows terminate all assigned processes (and their children, for example
the ComfyUI engine started by Lumen) as soon as the hub process ends, however it ends.
"""
from __future__ import annotations

import ctypes
import sys

_job = None


def _create() -> None:
    global _job
    if sys.platform != "win32" or _job is not None:
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                          ctypes.sizeof(info))
    if not ok:
        kernel32.CloseHandle(job)
        return
    _job = (kernel32, job)


CREATE_SUSPENDED = 0x00000004


def _resume(pid: int) -> None:
    """Resume every thread of a process created with CREATE_SUSPENDED (there is exactly one)."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPTHREAD = 0x4
    THREAD_SUSPEND_RESUME = 0x0002

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.OpenThread.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = kernel32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                th = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if th:
                    try:
                        while kernel32.ResumeThread(th) > 1:
                            pass
                    finally:
                        kernel32.CloseHandle(th)
            entry.dwSize = ctypes.sizeof(THREADENTRY32)
            ok = kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)


def popen_in_job(argv, **kwargs):
    """subprocess.Popen that is a member of the hub's job before it runs its first instruction.

    A venv's python.exe on Windows is a small launcher that immediately spawns the real interpreter;
    assigning the launcher *after* it started would let that child escape the job. Creating the
    process suspended, assigning it and only then resuming it closes that gap: every descendant
    (the real interpreter, ComfyUI, workers) is born inside the job and dies with the hub.
    """
    import subprocess

    flags = kwargs.pop("creationflags", 0)
    if sys.platform != "win32":
        return subprocess.Popen(argv, creationflags=flags, **kwargs), False
    try:
        _create()
    except Exception:
        pass
    if _job is None:
        return subprocess.Popen(argv, creationflags=flags, **kwargs), False
    proc = subprocess.Popen(argv, creationflags=flags | CREATE_SUSPENDED, **kwargs)
    ok = assign(proc.pid)
    try:
        _resume(proc.pid)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    return proc, ok


def assign(pid: int) -> bool:
    """Put a process into the hub's job. Returns False when unsupported (non-Windows or denied)."""
    try:
        _create()
    except Exception:
        return False
    if _job is None:
        return False
    kernel32, job = _job
    PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
    handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)
