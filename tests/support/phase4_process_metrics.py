"""Collect lightweight, JSON-safe metrics for the current evaluation process.

Call this inside the Python runner being measured, passing that runner's
``time.perf_counter()`` start value. No process is spawned and this module does
not import or initialize PyTorch/CUDA.
"""

from __future__ import annotations

import math
import os
import platform
import sys
import time
from typing import Any


def _windows_process_memory() -> dict[str, Any]:
    """Read working-set counters for this process with the Windows API."""
    result: dict[str, Any] = {
        "working_set_bytes": None,
        "peak_working_set_bytes": None,
        "unit": "bytes",
        "source": None,
        "reason": None,
    }
    if os.name != "nt":
        result["reason"] = "windows_process_counters_unavailable_on_this_os"
        return result

    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_memory_info = psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCountersEx),
            wintypes.DWORD,
        ]
        get_memory_info.restype = wintypes.BOOL

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        ok = get_memory_info(
            get_current_process(), ctypes.byref(counters), counters.cb
        )
        if not ok:
            error_code = ctypes.get_last_error()
            result["reason"] = f"GetProcessMemoryInfo_failed_winerror_{error_code}"
            return result

        result.update(
            working_set_bytes=int(counters.WorkingSetSize),
            peak_working_set_bytes=int(counters.PeakWorkingSetSize),
            source="GetProcessMemoryInfo",
        )
    except Exception as error:  # OS/library failures become data, not runner failures.
        result["reason"] = f"windows_process_counters_error_{type(error).__name__}"
    return result


def _loaded_torch_cuda_memory() -> dict[str, Any]:
    """Read CUDA counters only when torch is already imported and initialized."""
    result: dict[str, Any] = {
        "available": False,
        "device_index": None,
        "device_name": None,
        "allocated_bytes": None,
        "reserved_bytes": None,
        "peak_allocated_bytes": None,
        "peak_reserved_bytes": None,
        "unit": "bytes",
        "peak_scope": "since_torch_cuda_peak_counters_were_last_reset",
        "reason": None,
    }
    torch_module = sys.modules.get("torch")
    if torch_module is None:
        result["reason"] = "torch_not_already_loaded"
        return result

    cuda = getattr(torch_module, "cuda", None)
    if cuda is None:
        result["reason"] = "torch_cuda_module_unavailable"
        return result
    try:
        if not cuda.is_initialized():
            result["reason"] = "torch_cuda_not_initialized"
            return result
        device_index = int(cuda.current_device())
        result.update(
            available=True,
            device_index=device_index,
            device_name=str(cuda.get_device_name(device_index)),
            allocated_bytes=int(cuda.memory_allocated(device_index)),
            reserved_bytes=int(cuda.memory_reserved(device_index)),
            peak_allocated_bytes=int(cuda.max_memory_allocated(device_index)),
            peak_reserved_bytes=int(cuda.max_memory_reserved(device_index)),
        )
    except Exception as error:  # Do not let telemetry break the measured run.
        result.update(
            available=False,
            device_index=None,
            device_name=None,
            allocated_bytes=None,
            reserved_bytes=None,
            peak_allocated_bytes=None,
            peak_reserved_bytes=None,
            reason=f"torch_cuda_metrics_error_{type(error).__name__}",
        )
    return result


def collect_process_metrics(run_started_at: float | None = None) -> dict[str, Any]:
    """Return metrics for the actual current Python process.

    ``run_started_at`` must be a ``time.perf_counter()`` value captured by this
    same process when the measured run began. When omitted or invalid, elapsed
    time is ``null`` with a reason rather than a misleading launcher duration.
    Memory failures are likewise reported as ``null`` plus a reason.
    """
    captured_at = time.perf_counter()
    elapsed_seconds: float | None = None
    elapsed_reason: str | None = None
    if isinstance(run_started_at, (int, float)) and not isinstance(run_started_at, bool):
        if math.isfinite(float(run_started_at)) and float(run_started_at) <= captured_at:
            elapsed_seconds = round(captured_at - float(run_started_at), 6)
        else:
            elapsed_reason = "run_started_at_is_invalid_or_in_the_future"
    else:
        elapsed_reason = "run_started_at_not_provided"

    return {
        "pid": os.getpid(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "elapsed_seconds": elapsed_seconds,
        "elapsed_reason": elapsed_reason,
        "process_memory": _windows_process_memory(),
        "torch_cuda_memory": _loaded_torch_cuda_memory(),
    }
