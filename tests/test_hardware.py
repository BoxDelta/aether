"""GPU/RAM probe (#17): NVML mocked, nvidia-smi mocked, both missing."""

import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from router import hardware
from router.hardware import (
    MIB,
    MIN_FREE_VRAM_BYTES,
    UNKNOWN_GPU,
    GpuState,
    HardwareSnapshot,
    has_local_headroom,
    parse_meminfo,
    parse_nvidia_smi,
    probe,
    probe_gpu,
    probe_ram,
)
from tests.fakes import GIB


class FakeNvmlError(Exception):
    pass


def fake_nvml(
    *,
    total: int = 8 * GIB,
    free: int = 3 * GIB,
    util: int = 12,
    fail_init: bool = False,
    fail_memory: bool = False,
    fail_util: bool = False,
) -> tuple[ModuleType, list[str]]:
    calls: list[str] = []
    mod = ModuleType("pynvml")

    def init() -> None:
        calls.append("init")
        if fail_init:
            raise FakeNvmlError("Driver Not Loaded")

    def handle(index: int) -> str:
        calls.append(f"handle:{index}")
        return "gpu0"

    def memory(_h: str) -> SimpleNamespace:
        if fail_memory:
            raise FakeNvmlError("GPU is lost")
        return SimpleNamespace(total=total, free=free, used=total - free)

    def utilization(_h: str) -> SimpleNamespace:
        if fail_util:
            raise FakeNvmlError("Not Supported")
        return SimpleNamespace(gpu=util, memory=5)

    def shutdown() -> None:
        calls.append("shutdown")

    mod.nvmlInit = init  # type: ignore[attr-defined]
    mod.nvmlDeviceGetHandleByIndex = handle  # type: ignore[attr-defined]
    mod.nvmlDeviceGetMemoryInfo = memory  # type: ignore[attr-defined]
    mod.nvmlDeviceGetUtilizationRates = utilization  # type: ignore[attr-defined]
    mod.nvmlShutdown = shutdown  # type: ignore[attr-defined]
    return mod, calls


def use_nvml(monkeypatch: pytest.MonkeyPatch, mod: ModuleType | None) -> None:
    def load() -> ModuleType:
        if mod is None:
            raise ModuleNotFoundError("No module named 'pynvml'")
        return mod

    monkeypatch.setattr(hardware, "_load_nvml", load)


def use_smi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str | None = None,
    exc: BaseException | None = None,
) -> list[list[str]]:
    """stdout=None and exc=None means nvidia-smi is not installed."""
    seen: list[list[str]] = []
    installed = stdout is not None or exc is not None
    monkeypatch.setattr(
        "router.hardware.shutil.which", lambda name: "/usr/bin/nvidia-smi" if installed else None
    )

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        assert kwargs["timeout"] == hardware.NVIDIA_SMI_TIMEOUT_S
        assert "shell" not in kwargs
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("router.hardware.subprocess.run", run)
    return seen


# --- NVML ---------------------------------------------------------------------


def test_nvml_preferred_and_shut_down(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, calls = fake_nvml(total=8 * GIB, free=3 * GIB, util=12)
    use_nvml(monkeypatch, mod)
    smi = use_smi(monkeypatch, stdout="8192, 100, 99\n")

    assert probe_gpu() == GpuState("nvml", 8 * GIB, 3 * GIB, 12)
    assert calls == ["init", "handle:0", "shutdown"]
    assert smi == []  # fallback not consulted


def test_nvml_without_utilization_still_reports_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, _ = fake_nvml(fail_util=True)
    use_nvml(monkeypatch, mod)
    use_smi(monkeypatch)

    assert probe_gpu() == GpuState("nvml", 8 * GIB, 3 * GIB, None)


def test_nvml_init_failure_falls_back_to_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, calls = fake_nvml(fail_init=True)
    use_nvml(monkeypatch, mod)
    use_smi(monkeypatch, stdout="8192, 2048, 40\n")

    assert probe_gpu() == GpuState("nvidia-smi", 8192 * MIB, 2048 * MIB, 40)
    assert calls == ["init"]  # no shutdown without a successful init


def test_nvml_query_failure_shuts_down_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, calls = fake_nvml(fail_memory=True)
    use_nvml(monkeypatch, mod)
    use_smi(monkeypatch, stdout="8192, 2048, 40\n")

    assert probe_gpu().source == "nvidia-smi"
    assert calls[-1] == "shutdown"


# --- nvidia-smi ---------------------------------------------------------------


def test_smi_used_when_nvml_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    use_nvml(monkeypatch, None)
    seen = use_smi(monkeypatch, stdout="8192, 1692, 34\n")

    assert probe_gpu() == GpuState("nvidia-smi", 8192 * MIB, 1692 * MIB, 34)
    assert seen == [
        [
            "/usr/bin/nvidia-smi",
            "--query-gpu=memory.total,memory.free,utilization.gpu",
            "--format=csv,noheader,nounits",
        ]
    ]


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(["nvidia-smi"], 2.0),
        subprocess.CalledProcessError(9, ["nvidia-smi"]),
        PermissionError("denied"),
    ],
)
def test_smi_errors_give_unknown(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    use_nvml(monkeypatch, None)
    use_smi(monkeypatch, exc=exc)

    assert probe_gpu() == UNKNOWN_GPU


def test_smi_unparseable_output_gives_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    use_nvml(monkeypatch, None)
    use_smi(monkeypatch, stdout="NVIDIA-SMI has failed because it couldn't communicate\n")

    assert probe_gpu() == UNKNOWN_GPU


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("8192, 1692, 34\n", GpuState("nvidia-smi", 8192 * MIB, 1692 * MIB, 34)),
        ("8192, 1692, [N/A]\n", GpuState("nvidia-smi", 8192 * MIB, 1692 * MIB, None)),
        # Two GPUs: only GPU 0 is used.
        ("8192, 1000, 5\n6144, 6000, 0\n", GpuState("nvidia-smi", 8192 * MIB, 1000 * MIB, 5)),
        ("\n8192,1692,34", GpuState("nvidia-smi", 8192 * MIB, 1692 * MIB, 34)),
        ("", None),
        ("8192, 1692\n", None),
        ("[N/A], [N/A], 34\n", None),
        ("8192 MiB, 1692 MiB, 34 %\n", None),  # units not stripped: refuse to guess
        # "²" passes str.isdigit() but int() raises; other non-ASCII digits are
        # refused too rather than guessed at. The probe never raises (#121).
        ("8192, 1692, \u00b2\n", GpuState("nvidia-smi", 8192 * MIB, 1692 * MIB, None)),
        ("\uff18\uff11\uff19\uff12, 1692, 34\n", None),  # fullwidth digits
        ("8192, \u0661\u0666\u0669\u0662, 34\n", None),  # Arabic-Indic digits
    ],
)
def test_parse_nvidia_smi(output: str, expected: GpuState | None) -> None:
    assert parse_nvidia_smi(output) == expected


# --- both missing -------------------------------------------------------------


def test_both_missing_gives_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    use_nvml(monkeypatch, None)
    use_smi(monkeypatch)

    gpu = probe_gpu()
    assert gpu == UNKNOWN_GPU
    assert gpu.source == "unknown"
    assert gpu.vram_free_bytes is None


# --- RAM ----------------------------------------------------------------------

MEMINFO = """MemTotal:       32768000 kB
MemFree:         1000000 kB
MemAvailable:   20000000 kB
Buffers:          500000 kB
"""


def test_parse_meminfo_uses_memavailable() -> None:
    assert parse_meminfo(MEMINFO) == 20000000 * 1024


@pytest.mark.parametrize("text", ["", "MemTotal: 1 kB\n", "MemAvailable: lots kB\n"])
def test_parse_meminfo_unknown(text: str) -> None:
    assert parse_meminfo(text) is None


def test_probe_ram_reads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO, encoding="ascii")
    monkeypatch.setattr(hardware, "MEMINFO_PATH", path)

    assert probe_ram() == 20000000 * 1024


def test_probe_ram_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hardware, "MEMINFO_PATH", tmp_path / "absent")

    assert probe_ram() is None


def test_probe_combines_gpu_and_ram(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod, _ = fake_nvml()
    use_nvml(monkeypatch, mod)
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO, encoding="ascii")
    monkeypatch.setattr(hardware, "MEMINFO_PATH", path)

    snap = probe()
    assert snap.gpu.source == "nvml"
    assert snap.ram_available_bytes == 20000000 * 1024


# --- headroom -----------------------------------------------------------------


def snapshot(free: int | None) -> HardwareSnapshot:
    gpu = UNKNOWN_GPU if free is None else GpuState("nvml", 8 * GIB, free, 10)
    return HardwareSnapshot(gpu=gpu, ram_available_bytes=16 * GIB)


def test_reserve_is_charter_target() -> None:
    assert MIN_FREE_VRAM_BYTES == 1536 * MIB  # 1.5 GiB of the 8 GB card


def test_headroom_when_reserve_met() -> None:
    result = has_local_headroom(snapshot(MIN_FREE_VRAM_BYTES))
    assert result.ok
    assert result.reason == "free VRAM 1536 MiB meets reserve 1536 MiB"


def test_no_headroom_below_reserve() -> None:
    result = has_local_headroom(snapshot(MIN_FREE_VRAM_BYTES - 1))
    assert not result.ok
    assert "below reserve 1536 MiB" in result.reason


def test_no_headroom_when_unknown() -> None:
    result = has_local_headroom(snapshot(None))
    assert not result.ok
    assert result.reason == "GPU state unknown"


def test_custom_reserve() -> None:
    assert has_local_headroom(snapshot(3 * GIB), min_free_vram_bytes=4 * GIB).ok is False
    assert has_local_headroom(snapshot(3 * GIB), min_free_vram_bytes=2 * GIB).ok is True


def test_nvidia_smi_fallback_is_probed_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # No cache: each routing decision sees the GPU as it is now (#121).
    use_nvml(monkeypatch, None)
    seen = use_smi(monkeypatch, stdout="8192, 2048, 40\n")
    for _ in range(3):
        assert probe_gpu().source == "nvidia-smi"
    assert len(seen) == 3


# --- the local model's own allocation (ADR-040) --------------------------------------


def nvml_with_processes(
    procs: Sequence[tuple[int, int | None, str | bytes]], **kw: Any
) -> ModuleType:
    mod, _ = fake_nvml(**kw)
    names = {pid: name for pid, _, name in procs}
    mod.nvmlDeviceGetComputeRunningProcesses = lambda _h: [  # type: ignore[attr-defined]
        SimpleNamespace(pid=pid, usedGpuMemory=used) for pid, used, _ in procs
    ]
    mod.nvmlSystemGetProcessName = lambda pid: names[pid]  # type: ignore[attr-defined]
    return mod


def test_nvml_counts_the_llama_server_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = nvml_with_processes(
        [
            (10, 6006 * MIB, b"/app/llama-server"),
            (11, 300 * MIB, "/usr/lib/Xorg"),
            (12, None, b"/app/llama-server"),  # N/A inside some containers: skipped
        ],
        free=1131 * MIB,
    )
    use_nvml(monkeypatch, mod)

    assert probe_gpu().local_model_bytes == 6006 * MIB


@pytest.mark.parametrize(
    "procs",
    [[], [(11, 300 * MIB, "/usr/lib/Xorg")], [(12, 5 * MIB, "/opt/not-llama-server-x")]],
)
def test_no_llama_server_means_no_allocation(
    monkeypatch: pytest.MonkeyPatch, procs: Sequence[tuple[int, int | None, str]]
) -> None:
    use_nvml(monkeypatch, nvml_with_processes(procs))

    assert probe_gpu().local_model_bytes is None


def test_a_failing_process_list_keeps_the_memory_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    mod, _ = fake_nvml(free=2 * GIB)  # fake_nvml has no process functions at all
    use_nvml(monkeypatch, mod)

    state = probe_gpu()

    assert state.vram_free_bytes == 2 * GIB and state.local_model_bytes is None


def loaded(free_mib: int, model_mib: int | None) -> HardwareSnapshot:
    gpu = GpuState("nvml", 8 * GIB, free_mib * MIB, 5, model_mib * MIB if model_mib else None)
    return HardwareSnapshot(gpu=gpu, ram_available_bytes=16 * GIB)


def test_the_loaded_model_does_not_block_local_routing() -> None:
    # ADR-040 / tests/local-model/README.md §2.3: 1,131 MiB free with Qwen loaded.
    assert not has_local_headroom(loaded(1131, None)).ok  # not counted: blocked, as before
    result = has_local_headroom(loaded(1131, 6006), count_local_model=True)
    assert result.ok
    assert result.reason == (
        "free VRAM 7137 MiB (includes the local model's 6006 MiB) meets reserve 1536 MiB"
    )


def test_other_gpu_users_still_count_while_the_model_is_loaded() -> None:
    # Another program holds nearly everything the model does not: no room.
    result = has_local_headroom(loaded(0, 1000))
    assert not result.ok and "below reserve 1536 MiB" in result.reason


def test_not_loaded_is_unchanged() -> None:
    assert has_local_headroom(loaded(1536, None)).ok
    assert not has_local_headroom(loaded(1535, None)).ok
    assert has_local_headroom(snapshot(None)).reason == "GPU state unknown"


def test_the_model_is_counted_only_when_asked() -> None:
    snap = loaded(1131, 6006)  # another local provider or the dispatcher: plain check
    assert not has_local_headroom(snap).ok
    assert has_local_headroom(snap, count_local_model=True).ok


def test_cold_start_needs_the_model_plus_the_reserve() -> None:
    model = 5200 * MIB
    ok = has_local_headroom(loaded(6736, None), count_local_model=True, model_bytes=model)
    assert ok.ok and "model not loaded" in ok.reason
    no = has_local_headroom(loaded(6735, None), count_local_model=True, model_bytes=model)
    assert not no.ok and "below model 5200 MiB plus reserve 1536 MiB" in no.reason
    # Loaded: the counted behaviour (#277), whatever model_bytes says.
    assert has_local_headroom(loaded(1131, 6006), count_local_model=True, model_bytes=model).ok
    # Flag off (other providers, dispatcher): reserve only, model size ignored.
    assert has_local_headroom(loaded(2000, None), model_bytes=model).ok
