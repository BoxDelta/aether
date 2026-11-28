"""The owner signature on bot-made issues (ADR-042, step 4d).

The Discord bot creates an issue as the machine user, then edits its body once to end
with one line, ``<!-- aether-proof v2 ts nonce sig -->``. ``sig`` is an Ed25519
signature over a message that binds the repository, the issue number, the owner's
Discord ID, the route, a time, a nonce, the exact title and the exact body text before
the proof line. Copying a title and body to another issue, or editing either, breaks
it. Only the bot's Linux user holds the private key; everything here that runs as the
owner (the dispatcher, ``verify``, tests) holds only the public key, which sits in a
root-owned file at a fixed path. No environment variable changes where that is.

``cryptography`` is imported lazily. If it is missing, every check says "not valid":
bot issues are skipped and nothing else changes.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import stat
import struct
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

# Constants, never read from the API or the environment (ADR-042 decision 6).
REPOSITORY = "BoxDelta/aether"
ROUTE = "claude"
VERSION = "v2"
DOMAIN = b"aether-proof-v2"
PUBLIC_KEY_PATH = Path("/etc/aether-dispatch/proof.pub")
OWNER_ID_PATH = Path("/etc/aether-dispatch/owner_id")

MAX_AGE_SECONDS = 7 * 24 * 3600
MAX_FUTURE_SECONDS = 5 * 60

MAX_TITLE = 120
MAX_DETAILS = 2000
MAX_FILES = 5
MAX_FILE_NAME = 200
MAX_KEY_FILE_BYTES = 4096

# What the bot refuses in a title, details or file name: it could fake or end a proof
# line, or an HTML comment.
FORBIDDEN_TEXT = ("<!--", "-->", "aether-proof")

_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

_PROOF_LINE = re.compile(
    r"<!-- aether-proof v2 (?P<ts>0|[1-9][0-9]{0,11}) (?P<nonce>[0-9a-f]{32}) "
    r"(?P<sig>[A-Za-z0-9_-]{86}) -->"
)


@dataclass(frozen=True)
class Result:
    """A yes or no, with a reason that never repeats issue text."""

    ok: bool
    reason: str


def _no(reason: str) -> Result:
    return Result(False, reason)


def _field(value: str) -> bytes:
    """A field as UTF-8 bytes with a fixed-width 8-byte big-endian length in front."""
    raw = value.encode("utf-8")
    return struct.pack(">Q", len(raw)) + raw


@dataclass(frozen=True)
class Signed:
    """Everything the signature covers, in signing order (ADR-042 decision 6)."""

    number: int
    owner_id: str
    ts: int
    nonce: str
    title: str
    text: str
    repository: str = REPOSITORY
    route: str = ROUTE


def message(signed: Signed) -> bytes:
    """The bytes that are signed. The field order is fixed."""
    parts = (
        signed.repository,
        str(signed.number),
        signed.owner_id,
        signed.route,
        str(signed.ts),
        signed.nonce,
        signed.title,
        signed.text,
    )
    return DOMAIN + b"".join(_field(part) for part in parts)


def proof_line(ts: int, nonce: str, signature: bytes) -> str:
    sig = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"<!-- aether-proof {VERSION} {ts} {nonce} {sig} -->"


# --- the bot's side: what may be signed, and signing -----------------------------------


def _bad_text(label: str, value: str) -> str | None:
    lowered = value.lower()
    if any(bad in lowered for bad in FORBIDDEN_TEXT):
        return f"the {label} contains text that is not allowed"
    if "\r" in value:
        return f"the {label} contains a carriage return"
    allowed = "\n\t" if label == "details" else ""
    # Control, format (bidi marks, zero-width), surrogate, private-use and unassigned code
    # points, and line or paragraph separators could make
    # the public issue look different from the text that was signed.
    if any(c not in allowed and unicodedata.category(c) in _HIDDEN for c in value):
        return f"the {label} contains a control character"
    return None


def _size_problem(title: str, details: str, files: Sequence[str]) -> str | None:
    if not title.strip():
        return "the title is empty"
    if len(title.strip()) > MAX_TITLE:
        return "the title is too long"
    if len(details) > MAX_DETAILS:
        return "the details are too long"
    if len(files) > MAX_FILES:
        return "too many files"
    return None


def check_input(title: str, details: str, files: Sequence[str]) -> str | None:
    """Why the bot must refuse this task, or None. The reason never repeats the text."""
    too_big = _size_problem(title, details, files)
    if too_big:
        return too_big
    named = [("title", title), ("details", details), *(("file", name) for name in files)]
    for label, value in named:
        problem = _bad_text(label, value)
        if problem:
            return problem
    if any(not n.strip() or len(n) > MAX_FILE_NAME or n != n.strip() for n in files):
        return "a file name is not valid"
    return None


def normalize_title(title: str) -> str:
    """GitHub trims a title, so the bot signs the trimmed one."""
    return title.strip()


def render_text(details: str, files: Sequence[str]) -> str:
    """The body text before the proof line, with LF endings."""
    parts = [details.strip()]
    if files:
        parts.append("Files:\n" + "\n".join(f"- {name}" for name in files))
    return "\n\n".join(p for p in parts if p)


def new_nonce() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True)
class Subject:
    """The issue the bot is signing: its number, its trimmed title and the body text."""

    number: int
    title: str
    text: str


def sign_body(
    key: Ed25519PrivateKey,
    subject: Subject,
    owner_id: str,
    *,
    ts: int | None = None,
    nonce: str | None = None,
) -> str:
    """The final body: the text, one LF, the proof line, and nothing after it."""
    when = int(time.time()) if ts is None else ts
    used = new_nonce() if nonce is None else nonce
    signed = Signed(subject.number, owner_id, when, used, subject.title, subject.text)
    return f"{subject.text}\n{proof_line(when, used, key.sign(message(signed)))}"


# --- the verifier's side: public key only ----------------------------------------------


def _ed25519() -> Any | None:
    """The cryptography module for Ed25519, or None when it is not installed."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        return None
    return ed25519


def _folder_problem(path: Path, trusted_uid: int) -> str | None:
    try:
        parent = path.parent.lstat()
    except OSError:
        return "the file is missing, or is a symbolic link"
    if not stat.S_ISDIR(parent.st_mode):
        return "the file is not in a real folder"
    if parent.st_uid != trusted_uid or parent.st_mode & 0o022:
        return "the file or its folder is not root-owned and write-protected"
    return None


def _read_descriptor(descriptor: int, trusted_uid: int) -> bytes | str:
    """Check the open file, then read at most one byte over the limit."""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return "the file is not a regular file"
        if info.st_uid != trusted_uid or info.st_mode & 0o022:
            return "the file or its folder is not root-owned and write-protected"
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_KEY_FILE_BYTES + 1)
    except OSError:
        return "the file cannot be read"
    return "the file is too large" if len(data) > MAX_KEY_FILE_BYTES else data


def _read_trusted(path: Path, trusted_uid: int) -> bytes | str:
    """The file's bytes, or a reason it can't be trusted. It must be a regular file
    owned by ``trusted_uid`` that nobody else can write, in a folder that is the same.
    The folder is checked before the file is opened (so a planted FIFO or device in a
    wrong folder is never opened). Symlinks (the file, or its folder) are refused, and
    the file checks run on the open descriptor, so it can't be swapped after the check.
    A file over the size limit is refused, not cut off."""
    problem = _folder_problem(path, trusted_uid)
    if problem:
        return problem
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "the file is missing, or is a symbolic link"
    try:
        return _read_descriptor(descriptor, trusted_uid)
    finally:
        os.close(descriptor)


def load_public_key(
    path: Path = PUBLIC_KEY_PATH, *, trusted_uid: int = 0
) -> Ed25519PublicKey | str:
    """The public key, or a reason it could not be used. The default path is fixed."""
    if _ed25519() is None:
        return "cryptography is not installed"
    data = _read_trusted(path, trusted_uid)
    if isinstance(data, str):
        return f"public key: {data}"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        key = serialization.load_pem_public_key(data)
    except ValueError:
        return "public key: cannot be read"
    if not isinstance(key, Ed25519PublicKey):
        return "public key: not an Ed25519 key"
    return key


def load_owner_id(path: Path = OWNER_ID_PATH, *, trusted_uid: int = 0) -> str | None:
    """The owner's numeric Discord ID from the root-owned file, or None."""
    data = _read_trusted(path, trusted_uid)
    if isinstance(data, str):
        return None
    try:
        value = data.decode("ascii").strip()
    except ValueError:
        return None
    return value if re.fullmatch(r"[0-9]{1,20}", value) else None


def split_body(body: str | None) -> tuple[str, int, str, bytes] | None:
    """(text, ts, nonce, signature) when the body is ``text``, one LF, a well-formed
    proof line and nothing after it; else None."""
    if not body or "\r" in body:
        return None
    text, sep, last = body.rpartition("\n")
    if not sep:
        return None
    match = _PROOF_LINE.fullmatch(last)
    if match is None:
        return None
    sig = match["sig"]
    try:
        raw = base64.urlsafe_b64decode(sig + "==")
    except ValueError:
        return None
    if len(raw) != 64 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != sig:
        return None
    return text, int(match["ts"]), match["nonce"], raw


@dataclass(frozen=True)
class IssueView:
    """What the verifier read from GitHub, once, for this check."""

    number: int
    title: str
    body: str | None
    author: str
    is_pull_request: bool


@dataclass(frozen=True)
class Trust:
    """What the verifier trusts: the bot's login, the public key and the owner's ID."""

    bot_login: str
    public_key: Ed25519PublicKey
    owner_id: str


def _time_problem(ts: int, now: float) -> str | None:
    if ts > now + MAX_FUTURE_SECONDS:
        return "the proof is dated in the future"
    if now - ts > MAX_AGE_SECONDS:
        return "the proof is older than 7 days"
    return None


def _origin_problem(issue: IssueView, bot_login: str) -> str | None:
    if issue.author != bot_login:
        return "the issue was not opened by the bot"
    if issue.is_pull_request:
        return "the item is a pull request"
    return None


def verify(issue: IssueView, trust: Trust, now: float | None = None) -> Result:
    """Is this issue one the bot made for the owner? Checks the author, that it is an
    issue, the proof line, the time window and the signature, in that order."""
    wrong = _origin_problem(issue, trust.bot_login)
    if wrong:
        return _no(wrong)
    parts = split_body(issue.body)
    if parts is None:
        return _no("no well-formed proof line at the end of the body")
    text, ts, nonce, signature = parts
    late = _time_problem(ts, time.time() if now is None else now)
    if late:
        return _no(late)
    if _ed25519() is None:
        return _no("cryptography is not installed")
    from cryptography.exceptions import InvalidSignature

    signed = Signed(issue.number, trust.owner_id, ts, nonce, issue.title, text)
    try:
        trust.public_key.verify(signature, message(signed))
    except InvalidSignature:
        return _no("the signature does not match this issue")
    return Result(True, "valid")
