from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.app import create_app
from core.config import Settings
from core.identity import DEFAULT_IDENTITY_PATH, Identity, IdentityError, load_identity

VALID: dict[str, Any] = {
    "name": "Aether",
    "version": 3,
    "role": "Test role.",
    "tone": "Direct.",
    "principles": ["Report what actually happened."],
    "user_context_policy": {"include_by_default": ["public"]},
    "model_notes": {"ollama": "Keep it short."},
}


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "identity.yaml"
    path.write_text(yaml.safe_dump(data) if not isinstance(data, str) else data)
    return path


def with_changes(**changes: object) -> dict[str, Any]:
    data = dict(VALID)
    for key, value in changes.items():
        if value is None:
            data.pop(key)
        else:
            data[key] = value
    return data


# --- the real file -------------------------------------------------------------


def test_repository_identity_file_is_valid() -> None:
    identity = load_identity()

    assert DEFAULT_IDENTITY_PATH.name == "aether.yaml"
    assert identity.name == "Aether"
    assert identity.version >= 1
    assert len(identity.principles) == 5  # docs/07 section 3
    assert "secret" not in identity.user_context_policy.include_by_default


# --- valid input -------------------------------------------------------------


def test_valid_file_loads(tmp_path: Path) -> None:
    identity = load_identity(write(tmp_path, VALID))

    assert identity.version == 3
    assert identity.principles == ("Report what actually happened.",)
    assert identity.model_notes == {"ollama": "Keep it short."}


def test_model_notes_are_optional(tmp_path: Path) -> None:
    assert load_identity(write(tmp_path, with_changes(model_notes=None))).model_notes == {}


def test_identity_is_immutable(tmp_path: Path) -> None:
    identity = load_identity(write(tmp_path, VALID))

    with pytest.raises(ValidationError):
        identity.version = 4  # type: ignore[misc]


# --- invalid input ---------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["name", "version", "role", "tone", "principles", "user_context_policy"]
)
def test_missing_required_field_fails(tmp_path: Path, field: str) -> None:
    with pytest.raises(IdentityError, match=field):
        load_identity(write(tmp_path, with_changes(**{field: None})))


@pytest.mark.parametrize("version", [0, -1, "1", "1.0", 1.5, True])
def test_bad_version_fails(tmp_path: Path, version: object) -> None:
    with pytest.raises(IdentityError, match="version"):
        load_identity(write(tmp_path, with_changes(version=version)))


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "   "},
        {"tone": ""},
        {"principles": []},
        {"principles": ["ok", " "]},
        {"model_notes": {"ollama": ""}},
    ],
)
def test_blank_or_empty_values_fail(tmp_path: Path, changes: dict[str, object]) -> None:
    with pytest.raises(IdentityError):
        load_identity(write(tmp_path, with_changes(**changes)))


def test_unknown_field_fails(tmp_path: Path) -> None:
    with pytest.raises(IdentityError, match="voice"):
        load_identity(write(tmp_path, with_changes(voice="robotic")))


@pytest.mark.parametrize("level", ["secret", "Secret", "unknown"])
def test_disallowed_privacy_level_fails(tmp_path: Path, level: str) -> None:
    policy = {"include_by_default": ["public", level]}

    with pytest.raises(IdentityError, match="include_by_default"):
        load_identity(write(tmp_path, with_changes(user_context_policy=policy)))


def test_missing_file_fails_with_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"

    with pytest.raises(IdentityError, match=r"nope\.yaml"):
        load_identity(missing)


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    with pytest.raises(IdentityError, match="not valid YAML"):
        load_identity(write(tmp_path, "name: [unclosed\n"))


@pytest.mark.parametrize("content", ["", "- a list\n", "just a string\n"])
def test_non_mapping_fails(tmp_path: Path, content: str) -> None:
    with pytest.raises(IdentityError, match="mapping"):
        load_identity(write(tmp_path, content))


def test_yaml_tags_are_not_executed(tmp_path: Path) -> None:
    # safe_load must refuse arbitrary Python object construction.
    with pytest.raises(IdentityError, match="not valid YAML"):
        load_identity(write(tmp_path, "name: !!python/object/apply:os.getcwd []\n"))


# --- startup -------------------------------------------------------------------


def test_app_loads_default_identity_at_startup() -> None:
    app = create_app(Settings(_env_file=None))

    assert isinstance(app.state.identity, Identity)
    assert app.state.identity.name == "Aether"
    assert TestClient(app).get("/health").status_code == 200


def test_app_uses_given_identity(tmp_path: Path) -> None:
    identity = load_identity(write(tmp_path, VALID))

    app = create_app(Settings(_env_file=None), identity=identity)

    assert app.state.identity is identity
