"""GPU/RAM probe for Nexus routing (#17).

Reports free VRAM, GPU utilization and available RAM so routing can avoid local
inference when the desktop needs the GPU (Charter §8: keep at least 1.5 GB of the
8 GB VRAM free). GPU data comes from NVML (``nvidia-ml-py``) when it works, else
from parsing ``nvidia-smi``, else it is reported as unknown. Only GPU index 0 is
probed (the 8 GB VRAM GPU is the only GPU in the plan, docs/03 §3).

The probe is synchronous and can block for up to ``NVIDIA_SMI_TIMEOUT_S``; call it
with ``asyncio.to_thread`` from async code. It never raises.
"""

import importlib
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Literal

logger = logging.getLogger("aether.router.hardware")

MIB = 1024 * 1024
# Charter target "leave at least 1.5 GB free", read as GiB (the stricter reading).
MIN_FREE_VRAM_BYTES = 1536 * MIB
NVIDIA_SMI_TIMEOUT_S = 2.0
MEMINFO_PATH = Path("/proc/meminfo")

GpuSource = Literal["nvml", "nvidia-smi", "unknown"]


@dataclass(frozen=True, slots=True)
class GpuState:
    source: GpuSource
    vram_total_bytes: int | None = None
    vram_free_bytes: int | None = None
    # 0-100, None when the driver does not report it.
    utilization_percent: int | None = None


UNKNOWN_GPU = GpuState(source="unknown")


@dataclass(frozen=True, slots=True)
class HardwareSnapshot:
    gpu: GpuState
    # MemAvailable from /proc/meminfo (what the kernel can hand out without
    # swapping), None if unreadable.
    ram_available_bytes: int | None


@dataclass(frozen=True, slots=True)
class Headroom:
    ok: bool
    # Short, loggable explanation for the routing decision. No user content.
    reason: str


def _load_nvml() -> ModuleType:
    # Imported lazily: the package is untyped, and importing it must not fail Core
    # start-up on a machine without it. Tests replace this function.
    return importlib.import_module("pynvml")


def _probe_nvml() -> GpuState | None:
    try:
        nvml = _load_nvml()
        nvml.nvmlInit()
    except Exception as exc:
        logger.debug("NVML unavailable", extra={"reason": type(exc).__name__})
        return None
    try:
        handle = nvml.nvmlDeviceGetHandleByIndex(0)
        memory = nvml.nvmlDeviceGetMemoryInfo(handle)
        total, free = int(memory.total), int(memory.free)
        try:
            utilization: int | None = int(nvml.nvmlDeviceGetUtilizationRates(handle).gpu)
        except Exception:
            utilization = None  # some drivers/GPUs don't support it
        return GpuState("nvml", total, free, utilization)
    except Exception as exc:
        logger.debug("NVML query failed", extra={"reason": type(exc).__name__})
        return None
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            logger.debug("NVML shutdown failed")


def _parse_mib(field: str) -> int | None:
    field = field.strip()
    return int(field) * MIB if field.isascii() and field.isdigit() else None  # "[N/A]" etc.


def parse_nvidia_smi(output: str) -> GpuState | None:
    """Parse ``--query-gpu=memory.total,memory.free,utilization.gpu`` CSV
    (``noheader,nounits``). Uses the first line (GPU 0). None if unparseable."""
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    fields = lines[0].split(",")
    if len(fields) != 3:
        return None
    total, free = _parse_mib(fields[0]), _parse_mib(fields[1])
    if total is None or free is None:
        return None
    util_field = fields[2].strip()
    utilization = int(util_field) if util_field.isascii() and util_field.isdigit() else None
    return GpuState("nvidia-smi", total, free, utilization)


def _probe_nvidia_smi() -> GpuState | None:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        logger.debug("nvidia-smi not found")
        return None
    try:
        result = subprocess.run(  # noqa: S603 - absolute path from which(), fixed args
            [
                exe,
                "--query-gpu=memory.total,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi failed", extra={"reason": type(exc).__name__})
        return None
    state = parse_nvidia_smi(result.stdout)
    if state is None:
        logger.debug("nvidia-smi output not understood")
    return state


def probe_gpu() -> GpuState:
    """NVML, then nvidia-smi, else ``UNKNOWN_GPU``. Never raises."""
    return _probe_nvml() or _probe_nvidia_smi() or UNKNOWN_GPU


def parse_meminfo(text: str) -> int | None:
    """``MemAvailable`` in bytes from /proc/meminfo text, or None."""
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isascii() and parts[1].isdigit():
                return int(parts[1]) * 1024  # value is in kB (KiB)
            return None
    return None


def probe_ram() -> int | None:
    """Available RAM in bytes (Linux), None if unknown. Never raises."""
    try:
        return parse_meminfo(MEMINFO_PATH.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("meminfo unreadable", extra={"reason": type(exc).__name__})
        return None


def probe() -> HardwareSnapshot:
    return HardwareSnapshot(gpu=probe_gpu(), ram_available_bytes=probe_ram())


def has_local_headroom(
    snapshot: HardwareSnapshot, min_free_vram_bytes: int = MIN_FREE_VRAM_BYTES
) -> Headroom:
    """Whether local inference may run without eating into the desktop's VRAM.

    Unknown GPU state counts as *no* headroom: the Charter puts desktop
    responsiveness before local throughput, so routing prefers cloud unless it
    has a reason (privacy, manual override) to go local anyway.
    """
    free = snapshot.gpu.vram_free_bytes
    if free is None:
        return Headroom(False, "GPU state unknown")
    free_mib, need_mib = free // MIB, min_free_vram_bytes // MIB
    if free < min_free_vram_bytes:
        return Headroom(False, f"free VRAM {free_mib} MiB below reserve {need_mib} MiB")
    return Headroom(True, f"free VRAM {free_mib} MiB meets reserve {need_mib} MiB")
