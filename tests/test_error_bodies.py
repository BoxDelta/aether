"""Pin the exact error bodies Core returns (#31).

The bytes below were captured from ``main`` before the error format moved to
``core/errors.py``; any change to an error body must be deliberate.
"""

from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.app import create_app
from core.config import Settings
from core.identity import load_identity
from tests.chat_helpers import Setup, error_with_key_cause

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


def test_502_route_failed_body_is_pinned() -> None:
    s = Setup(
        local_error=error_with_key_cause("ollama"), cloud_error=error_with_key_cause("claude")
    )
    r = s.client.post(
        "/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "allow_cloud": True},
        headers=HEADERS,
    )
    assert r.status_code == 502
    assert r.content == (
        b'{"request_id":"pin-1","error":{"type":"RouteFailed",'
        b'"message":"fallback claude also failed (ProviderUnavailable)","attempts":['
        b'{"provider":"ollama","error_type":"ProviderUnavailable",'
        b'"message":"cannot connect to ollama"},'
        b'{"provider":"claude","error_type":"ProviderUnavailable",'
        b'"message":"cannot connect to claude"}'
        b"]}}"
    )


def test_503_auth_not_configured_body_is_pinned() -> None:
    # No AETHER_API_TOKEN: every protected route answers 503 AuthNotConfigured.
    router = cast(FastAPI, Setup().client.app).state.router
    app = create_app(Settings(_env_file=None), load_identity(), router=router)
    r = TestClient(app).post(
        "/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=HEADERS
    )
    assert r.status_code == 503
    assert r.content == (
        b'{"request_id":"pin-1","error":{"type":"AuthNotConfigured",'
        b'"message":"API token is not configured on the server","attempts":[]}}'
    )


def test_documented_422_model_has_fields() -> None:
    schema = TestClient(Setup().client.app).get("/openapi.json", headers=Setup().client.headers)
    error_info = schema.json()["components"]["schemas"]["ErrorInfo"]
    assert "fields" in error_info["properties"]
    assert "only on 422" in error_info["properties"]["fields"]["description"]
