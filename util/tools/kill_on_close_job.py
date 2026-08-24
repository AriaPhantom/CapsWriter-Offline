# coding: utf-8
"""
Windows Job Object 兜底回收子进程

为什么需要这个：识别子进程会加载 sherpa-onnx，单进程提交约 4GB
（OpenBLAS 按核心数预留线程栈，24 核机器上尤其明显）。以前它的存活
完全依赖父进程跑到 cleanup 里 terminate()，而父进程被 TerminateProcess
硬杀时（taskkill /F、任务管理器结束任务、崩溃）那段代码不会执行。

`daemon=True` **不足以**解决这个问题：multiprocessing 的 daemon 回收是在
父进程的 atexit 里做的，TerminateProcess 会直接跳过 atexit。实测确认
daemon=True 时子进程依然存活成为孤儿。

Job Object 配 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 是内核层面的保证：
父进程一死，它持有的最后一个 job 句柄被内核关闭，job 里所有进程
立即被终止 —— 不经过用户态代码，所以硬杀也拦不住。
"""

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# 进程级单例。这个句柄必须一直持有到进程退出：
# 一旦关闭（或被 GC），job 里的进程会立刻被杀。
_job_handle: Optional[int] = None
_job_unavailable = False


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def get_kill_on_close_job() -> Optional[int]:
    """
    取得（首次调用时创建）本进程的 KILL_ON_JOB_CLOSE job 句柄。

    非 Windows 平台、或创建失败时返回 None —— 调用方应当把这视为
    「没有兜底」而继续正常运行，不要因此让服务起不来。
    """
    global _job_handle, _job_unavailable

    if sys.platform != "win32" or _job_unavailable:
        return None
    if _job_handle is not None:
        return _job_handle

    try:
        k32 = _kernel32()
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.WinError(ctypes.get_last_error())
            k32.CloseHandle(handle)
            raise err

        _job_handle = handle
        return _job_handle
    except Exception:
        # 记不了日志（本模块刻意不依赖项目 logger，避免循环导入），
        # 交给调用方去记。
        _job_unavailable = True
        return None


def assign_process_to_job(pid: int) -> bool:
    """
    把 pid 加入本进程的 KILL_ON_JOB_CLOSE job。

    返回是否成功。失败不应中断调用方 —— 那只是失去了兜底保护。

    注意：进程已在另一个 job 里时可能失败（Win8 之前不支持嵌套 job）。
    这种情况下靠上层的显式 terminate() 即可，行为退回到修复前的水平。
    """
    if sys.platform != "win32":
        return False

    job = get_kill_on_close_job()
    if job is None:
        return False

    try:
        k32 = _kernel32()
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False
