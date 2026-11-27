"""AETHER identity: the versioned definition applied to every model call (docs/07).

The identity lives in ``identity/aether.yaml``. Core loads and validates it at
startup and refuses to start if it is invalid, so a bad edit is caught
immediately instead of silently changing the assistant's behaviour.
"""

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, ValidationError

from router.contract import NonSecretLevel

DEFAULT_IDENTITY_PATH = Path(__file__).resolve().parent.parent / "identity" / "aether.yaml"

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# The headings render_system_prompt starts its sections with. Memory text that
# contains one is neutralised before it enters the prompt (memory/retrieval.py), so
# a record can't appear to start a new section (ADR-024 §3).
TONE_HEADING = "Tone:"
PRINCIPLES_HEADING = "Principles:"
NOTES_HEADING = "Notes for this model:"
MEMORY_HEADING = "Relevant memory:"
TASK_HEADING = "Task instructions:"
SECTION_HEADINGS = (TONE_HEADING, PRINCIPLES_HEADING, NOTES_HEADING, MEMORY_HEADING, TASK_HEADING)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserContextPolicy(_Strict):
    # docs/05 §7.1: secret may never be included by default (#58: names from router.contract).
    include_by_default: tuple[NonSecretLevel, ...]


class Identity(_Strict):
    name: NonEmptyStr
    version: Annotated[StrictInt, Field(ge=1)]
    role: NonEmptyStr
    tone: NonEmptyStr
    principles: Annotated[tuple[NonEmptyStr, ...], Field(min_length=1)]
    user_context_policy: UserContextPolicy
    model_notes: dict[NonEmptyStr, NonEmptyStr] = Field(default_factory=dict)


class IdentityError(Exception):
    """The identity file is missing, unreadable or invalid."""


def load_identity(path: Path = DEFAULT_IDENTITY_PATH) -> Identity:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise IdentityError(f"cannot read identity file {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise IdentityError(f"identity file {path} is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise IdentityError(f"identity file {path} must contain a mapping at the top level")

    try:
        return Identity.model_validate(raw)
    except ValidationError as exc:
        raise IdentityError(f"identity file {path} is invalid:\n{exc}") from exc


def render_system_prompt(
    identity: Identity,
    *,
    provider: str | None = None,
    memory_context: str | None = None,
    task: str | None = None,
) -> str:
    """Build the system prompt for one model call (docs/07 §4).

    Order: identity → permitted memory context → task instructions. The identity
    part is the same for every provider; ``model_notes[provider]`` (hand-written in
    the same identity file, never anywhere else) is appended to it when present.
    """
    lines = [
        f"You are {identity.name}. {identity.role}",
        "",
        f"{TONE_HEADING} {identity.tone}",
        "",
        PRINCIPLES_HEADING,
        *(f"{i}. {p}" for i, p in enumerate(identity.principles, start=1)),
    ]
    note = identity.model_notes.get(provider) if provider else None
    if note:
        lines += ["", f"{NOTES_HEADING} {note}"]
    sections = ["\n".join(lines)]
    if memory_context and memory_context.strip():
        sections.append(f"{MEMORY_HEADING}\n{memory_context.strip()}")
    if task and task.strip():
        sections.append(f"{TASK_HEADING}\n{task.strip()}")
    return "\n\n".join(sections)
