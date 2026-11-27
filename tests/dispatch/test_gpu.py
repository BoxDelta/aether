"""Which processes hold the GPU (#131). Never raises; unknown is None."""

import subprocess

import pytest

from dispatch import gpu


@pytest.fixture
def has_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dispatch.gpu.shutil.which", lambda _: "/usr/bin/nvidia-smi")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("llama-server\n", True),
        ("/app/llama-server\n/app/llama-server\n", True),
        ("/app/llama-server\n/usr/bin/gnome-shell\n", False),
        ("ollama\n", False),
        ("", False),  # nothing loaded: check headroom as now
    ],
)
def test_only_our_model(has_smi: None, output: str, expected: bool) -> None:
    assert gpu.only_our_model(lambda args: output) is expected


def test_query_args(has_smi: None) -> None:
    seen: list[list[str]] = []

    def runner(args: list[str]) -> str:
        seen.append(args)
        return "llama-server\n"

    gpu.only_our_model(runner)
    assert seen == [
        ["/usr/bin/nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader"]
    ]


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("nvidia-smi", 2.0),
        subprocess.CalledProcessError(9, "nvidia-smi"),
        OSError("gone"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
    ],
)
def test_failure_is_unknown(has_smi: None, error: Exception) -> None:
    def runner(args: list[str]) -> str:
        raise error

    assert gpu.only_our_model(runner) is None


def test_missing_nvidia_smi_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dispatch.gpu.shutil.which", lambda _: None)
    assert gpu.only_our_model(lambda args: "llama-server\n") is None
