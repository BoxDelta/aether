"""Pin the exact error bodies Core returns (#31).

The bytes below were captured from ``main`` before the error format moved to
``core/errors.py``; any change to an error body must be deliberate.
"""

from fastapi.testclient import TestClient

from tests.test_chat import Setup

HEADERS = {"x-request-id": "pin-1"}
JSON = {"content-type": "application/json"}


def test_422_validation_body_is_pinned() -> None:
    s = Setup()
    r = s.client.post("/chat", json={"messages": [], "privacy": "nope"}, headers=HEADERS)
    assert r.status_code == 422
    assert r.content == (
        b'{"request_id":"pin-1","error":{"type":"ValidationError","message":"invalid request",'
        b'"attempts":[],"fields":[{"loc":["body","messages"],"msg":"List should have at least'
        b' 1 item after validation, not 0"},{"loc":["body","privacy"],"msg":"Input should be '
        b"'public', 'personal', 'sensitive' or 'secret'\"}]}}"
    )


def test_422_malformed_json_body_is_pinned() -> None:
    s = Setup()
    r = s.client.post("/chat", content=b'{"messages": [', headers={**HEADERS, **JSON})
    assert r.status_code == 422
    assert r.content == (
        b'{"request_id":"pin-1","error":{"type":"ValidationError","message":"invalid request",'
        b'"attempts":[],"fields":[{"loc":["body","14"],"msg":"JSON decode error"}]}}'
    )


def test_other_error_bodies_have_no_fields_key() -> None:
    s = Setup()
    too_large = s.client.post("/chat", content=b"x" * (256 * 1024 + 1), headers={**HEADERS, **JSON})
    unauthorized = TestClient(s.client.app).post(
        "/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=HEADERS
    )
    no_route = s.client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "privacy": "secret"},
        headers=HEADERS,
    )
    assert (too_large.status_code, too_large.content) == (
        413,
        b'{"request_id":"pin-1","error":{"type":"RequestTooLarge",'
        b'"message":"request body is over 262144 bytes","attempts":[]}}',
    )
    assert (unauthorized.status_code, unauthorized.content) == (
        401,
        b'{"request_id":"pin-1","error":{"type":"Unauthorized",'
        b'"message":"missing or invalid API token","attempts":[]}}',
    )
    assert (no_route.status_code, no_route.content) == (
        503,
        b'{"request_id":"pin-1","error":{"type":"NoRoute",'
        b'"message":"secret data is never sent to any model","attempts":[]}}',
    )


def test_documented_422_model_has_fields() -> None:
    schema = TestClient(Setup().client.app).get("/openapi.json", headers=Setup().client.headers)
    error_info = schema.json()["components"]["schemas"]["ErrorInfo"]
    assert "fields" in error_info["properties"]
