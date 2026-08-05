"""进程树 containment（worker-split-minimal 组 2，对标 Multica daemon 的 Job Object/cgroup）。

**目的**：worker 进程终止（尤其被强杀 taskkill /F、崩溃、断电）时，它派生的所有 CLI 子进程
（claude/codex 及其子孙）被 OS **连带清理**——保证「已死的 worker 不会再有子进程写数据库或
工作目录」。这是省掉大量 fencing token 的物理前提（Multica 减法校准 B3：containment 保证清理
必成，故 process_cleanup 二态可砍）。

**与 kill_run 的分工**：
  - `runner.kill_run`：主动 kill **单个** run 的进程树（taskkill /F /T + pid 身份校验），
    用于超时/用户终止/信号消费——精确、可控。
  - 本模块 containment：**兜底**——worker 整体死亡时，OS 自动清理其名下全部 CLI 子进程，
    无需 worker 有机会执行任何清理代码（强杀/崩溃时 kill_run 根本跑不到）。

**Windows 实现**：Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。worker 进程持有一个
Job 句柄，所有 CLI 子进程 `AssignProcessToJobObject` 加入；worker 进程一死，最后一个 Job 句柄
关闭，OS 立即终止 Job 内所有进程。纯标准库 ctypes 实现（不引 pywin32 依赖）。

**POSIX**：用进程组（子进程 start_new_session），worker 退出时杀进程组。当前平台为 Windows，
POSIX 分支留作接口占位（start_new_session 已在 kill_run 的 killpg 注释中约定）。

**降级**：Job Object 创建/assign 失败时不抛异常、只告警——退回依赖 `kill_run` 的 taskkill /T
逐个清理（现状），containment 是增强而非唯一防线。
"""
from __future__ import annotations

import ctypes
import os
import sys

# 进程级单例 Job 句柄（None = 未初始化 / 不支持 / 降级）。worker 进程持有它直到进程退出，
# 退出即触发 KILL_ON_JOB_CLOSE 清理。故意用模块级全局：Job 的生命周期 = worker 进程生命周期。
_JOB_HANDLE = None
_INITED = False

# Windows API 常量
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _is_windows() -> bool:
    return os.name == "nt"


def init_containment() -> bool:
    """worker 进程启动时调用一次：创建进程级 Job Object（KILL_ON_JOB_CLOSE）。

    幂等：重复调用只初始化一次。返回 True=containment 就绪；False=不支持/降级（依赖 kill_run 兜底）。
    """
    global _JOB_HANDLE, _INITED
    if _INITED:
        return _JOB_HANDLE is not None
    _INITED = True
    if not _is_windows():
        # POSIX：不建 Job；worker 起子进程时 start_new_session 建进程组，退出杀组（接口占位）。
        print("[containment] 非 Windows：Job Object 跳过，依赖进程组/ kill_run 兜底", flush=True)
        return False
    try:
        _JOB_HANDLE = _create_kill_on_close_job()
        if _JOB_HANDLE:
            print("[containment] Job Object 就绪（worker 死→CLI 子进程被 OS 连带清理）", flush=True)
            return True
    except Exception as e:  # noqa: BLE001 — 任何失败都降级，不阻断 worker 启动
        print(f"[containment] Job Object 初始化失败，降级依赖 kill_run 兜底："
              f"{type(e).__name__}: {e}", flush=True)
    _JOB_HANDLE = None
    return False


def contain(pid: int) -> bool:
    """把一个 CLI 子进程（pid）加入 worker 的 Job Object。

    在 subprocess.Popen 起进程后**立即**调用。返回 True=已纳入 containment；False=降级
    （Job 未就绪/assign 失败）——此时该进程仅靠 kill_run 的 taskkill /T 清理，功能不回退。
    失败绝不抛异常（containment 是增强，不能因它阻断执行）。
    """
    if _JOB_HANDLE is None or not _is_windows():
        return False
    try:
        return _assign_process_to_job(_JOB_HANDLE, pid)
    except Exception as e:  # noqa: BLE001
        print(f"[containment] assign pid={pid} 失败（降级 kill_run 兜底）："
              f"{type(e).__name__}: {e}", flush=True)
        return False


# ---------- Windows API（ctypes，纯标准库，无 pywin32） ----------

class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
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


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _create_kill_on_close_job():
    """创建一个 KILL_ON_JOB_CLOSE 的 Job Object，返回其句柄（失败抛异常）。"""
    k32 = _kernel32()
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    # 设置 KILL_ON_JOB_CLOSE：最后一个 Job 句柄关闭（= worker 进程死）时终止 Job 内所有进程。
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        ctypes.c_void_p(handle), _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        err = ctypes.get_last_error()
        k32.CloseHandle(ctypes.c_void_p(handle))
        raise ctypes.WinError(err)
    return handle


def _assign_process_to_job(job_handle, pid: int) -> bool:
    """把 pid 对应的进程加入 job。返回 True=成功。"""
    k32 = _kernel32()
    k32.OpenProcess.restype = ctypes.c_void_p
    # 需要 SET_QUOTA + TERMINATE 权限才能被 assign 进 Job。
    hproc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not hproc:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ok = k32.AssignProcessToJobObject(ctypes.c_void_p(job_handle), ctypes.c_void_p(hproc))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        k32.CloseHandle(ctypes.c_void_p(hproc))
