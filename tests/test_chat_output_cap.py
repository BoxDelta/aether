"""/chat and the cloud output token cap (#77): per-request limit, 422 above the cap,
and cut-off answers visible in the response and the event."""

from typing import Any

import pytest

from tests.test_chat import ANSWER, PROMPT, Setup, assert_no_content


def completed(rows: list[dict[str, Any]]) -> dict[str, Any]:
    [row] = [r for r in rows if r["event_type"] == "model.call.completed"]
    return row


def test_no_limit_by_default() -> None:
    s = Setup()

    assert s.post().status_code == 200
    assert s.local.requests[0].max_tokens is None  # the provider applies its cap


def test_request_limit_is_passed_to_the_provider() -> None:
    s = Setup()

    r = s.post(max_tokens=300)

    assert r.status_code == 200
    assert s.local.requests[0].max_tokens == 300


def test_request_limit_reaches_the_cloud_provider() -> None:
    s = Setup()

    r = s.post(max_tokens=300, allow_cloud=True, override="claude")

    assert r.status_code == 200
    assert s.cloud.requests[0].max_tokens == 300


def test_limit_equal_to_the_cap_is_allowed() -> None:
    s = Setup(cloud_cap="1000")

    assert s.post(max_tokens=1000).status_code == 200


def test_limit_above_the_default_cap_is_422_without_echo() -> None:
    s = Setup()

    r = s.post(max_tokens=4097)

    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "ValidationError"
    assert body["error"]["message"] == "invalid request"
    assert body["error"]["fields"] == [
        {"loc": ["body", "max_tokens"], "msg": "Input should be less than or equal to 4096"}
    ]
    assert "4097" not in r.text
    assert_no_content(r.text)
    assert s.local.requests == [] and s.cloud.requests == []
    assert s.rows == []  # rejected before any event


def test_limit_above_a_configured_cap_is_422() -> None:
    s = Setup(cloud_cap="1000")

    r = s.post(max_tokens=1001)

    assert r.status_code == 422
    assert "less than or equal to 1000" in r.text
    assert "1001" not in r.text


@pytest.mark.parametrize("bad", [0, -5, 12.5, "100", True, None])
def test_invalid_limits_are_422_without_echo(bad: Any) -> None:
    s = Setup()
    body: dict[str, Any] = {"messages": [{"role": "user", "content": PROMPT}]}
    body["max_tokens"] = bad

    r = s.client.post("/chat", json=body)

    if bad is None:  # explicit null means "no limit"
        assert r.status_code == 200
        return
    assert r.status_code == 422
    assert r.json()["error"]["fields"][0]["loc"] == ["body", "max_tokens"]
    assert_no_content(r.text)
    assert "12.5" not in r.text
    assert s.local.requests == []


def test_cut_off_answer_is_visible_and_recorded() -> None:
    s = Setup(finish_reason="length")

    r = s.post(allow_cloud=True, override="claude")

    assert r.status_code == 200
    body = r.json()
    assert body["finish_reason"] == "length"
    assert body["text"] == ANSWER  # partial text is returned, not dropped
    row = completed(s.rows)
    assert row["finish_reason"] == "length"
    assert row["outcome"] == "ok"
    assert row["output_tokens"] == 7


def test_cut_off_with_empty_text_is_visible_and_recorded() -> None:
    # Thinking can use the whole budget and leave no text: still a 200, never hidden.
    s = Setup(finish_reason="length", text="")

    r = s.post(allow_cloud=True, override="claude")

    assert r.status_code == 200
    body = r.json()
    assert body["text"] == ""
    assert body["finish_reason"] == "length"
    assert body["provider"] == "claude"
    assert body["output_tokens"] == 7
    row = completed(s.rows)
    assert row["finish_reason"] == "length"
    assert row["provider"] == "claude"
    assert row["outcome"] == "ok"
