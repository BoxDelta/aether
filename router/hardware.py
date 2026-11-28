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
# The process llama-swap starts for the local model (ADR-040), matched by exact name.
LOCAL_MODEL_PROCESS = "llama-server"
MEMINFO_PATH = Path("/proc/meminfo")

GpuSource = Literal["nvml", "nvidia-smi", "unknown"]


@dataclass(frozen=True, slots=True)
class GpuState:
    source: GpuSource
    vram_total_bytes: int | None = None
    vram_free_bytes: int | None = None
    # 0-100, None when the driver does not report it.
    utilization_percent: int | None = None
    # VRAM held by the local model's own process (``llama-server``, ADR-040), None
    # when it is not loaded or NVML can't tell. Only NVML reports it.
    local_model_bytes: int | None = None


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
        return GpuState("nvml", total, free, utilization, _local_model_bytes(nvml, handle))
    except Exception as exc:
        logger.debug("NVML query failed", extra={"reason": type(exc).__name__})
        return None
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            logger.debug("NVML shutdown failed")


def _local_model_bytes(nvml: ModuleType, handle: object) -> int | None:
    """VRAM used by processes named ``llama-server``, or None (not loaded, or NVML
    can't list processes or names). Never raises. The name is the only link to the
    model: another process with that name would only raise the room counted."""
    try:
        total = 0
        for proc in nvml.nvmlDeviceGetComputeRunningProcesses(handle):
            used = proc.usedGpuMemory
            if not isinstance(used, int) or used <= 0:
                continue  # NVML reports None/N/A inside some containers
            raw = nvml.nvmlSystemGetProcessName(proc.pid)
            name = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            if name.rsplit("/", 1)[-1] == LOCAL_MODEL_PROCESS:
                total += used
        return total or None
    except Exception as exc:
        logger.debug("NVML process list failed", extra={"reason": type(exc).__name__})
        return None


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
    snapshot: HardwareSnapshot,
    min_free_vram_bytes: int = MIN_FREE_VRAM_BYTES,
    *,
    count_local_model: bool = False,
    model_bytes: int = 0,
) -> Headroom:
    """Whether local inference may run without eating into the desktop's VRAM.

    Unknown GPU state counts as *no* headroom: the Charter puts desktop
    responsiveness before local throughput, so routing prefers cloud unless it
    has a reason (privacy, manual override) to go local anyway.

    With ``count_local_model`` (only for a request that goes to the llama-swap model
    itself, ADR-040) its own allocation counts as room it already has: the request
    takes no new VRAM, so a warm model must not block routing to itself. The room is
    free VRAM plus that allocation. Note this means the 1.5 GiB reserve cannot fail
    while the model is resident (room is then total VRAM minus other users); it only
    applies when the model is not loaded (#277: kept as built).

    With the flag and the model *not* loaded (or NVML can't see it), the request would
    load it, so free VRAM must cover ``model_bytes`` plus the reserve (ADR-040,
    cold start); otherwise there is no headroom and routing looks elsewhere. The flag
    off (other providers, the dispatcher): free VRAM against the reserve, as before.
    """
    free = snapshot.gpu.vram_free_bytes
    if free is None:
        return Headroom(False, "GPU state unknown")
    own = (snapshot.gpu.local_model_bytes or 0) if count_local_model else 0
    room = free + own
    if count_local_model and not own and model_bytes:
        needed = model_bytes + min_free_vram_bytes
        free_mib, need_mib = free // MIB, needed // MIB
        if free < needed:
            return Headroom(
                False,
                f"model not loaded: free VRAM {free_mib} MiB below model "
                f"{model_bytes // MIB} MiB plus reserve {min_free_vram_bytes // MIB} MiB",
            )
        return Headroom(
            True, f"model not loaded: free VRAM {free_mib} MiB meets {need_mib} MiB needed"
        )
    room_mib, need_mib = room // MIB, min_free_vram_bytes // MIB
    counted = f" (includes the local model's {own // MIB} MiB)" if own else ""
    if room < min_free_vram_bytes:
        return Headroom(False, f"free VRAM {room_mib} MiB{counted} below reserve {need_mib} MiB")
    return Headroom(True, f"free VRAM {room_mib} MiB{counted} meets reserve {need_mib} MiB")
