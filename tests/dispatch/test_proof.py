"""The owner signature on bot-made issues (ADR-042, dispatch/proof.py).

These need ``cryptography`` (the ``dispatch`` dependency group). They fail, not skip,
without it: a skipped check must be visible (docs/05 §10).
"""

import base64
import os
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from dispatch import proof

BOT = "boxdelta-dispatch"
OWNER = "123456789012345678"
TS = 1795900000
NONCE = "00112233445566778899aabbccddeeff"
TITLE = "Fix the flaky test"
TEXT = "Make the probe longer.\n\nFiles:\n- tests/test_memory_api.py"
NUMBER = 228

# A fixed key (RFC 8032 signatures are deterministic), so these vectors never change.
KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
PUBLIC_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAA6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=\n"
    "-----END PUBLIC KEY-----\n"
)
VECTOR_BODY = (
    "Make the probe longer.\n\nFiles:\n- tests/test_memory_api.py\n"
    "<!-- aether-proof v2 1795900000 00112233445566778899aabbccddeeff "
    "Mq7RWJV9cHISD2pW6rZJ9S2yA0gcWGz4tQoshp36CUXt7QbLH_1-GtMOL3P9qg1c78b8kva6G_jOcVULknG_BQ -->"
)


def signed(**over: object) -> proof.Signed:
    args: dict[str, object] = {
        "number": NUMBER,
        "owner_id": OWNER,
        "ts": TS,
        "nonce": NONCE,
        "title": TITLE,
        "text": TEXT,
    }
    args.update(over)
    return proof.Signed(**args)  # type: ignore[arg-type]


def body_for(**over: object) -> str:
    s = signed(**over)
    subject = proof.Subject(s.number, s.title, s.text)
    return proof.sign_body(KEY, subject, s.owner_id, ts=s.ts, nonce=s.nonce)


def check(**over: object) -> proof.Result:
    args: dict[str, object] = {
        "number": NUMBER,
        "title": TITLE,
        "body": VECTOR_BODY,
        "author": BOT,
        "is_pull_request": False,
    }
    trust: dict[str, object] = {
        "bot_login": BOT,
        "public_key": KEY.public_key(),
        "owner_id": OWNER,
    }
    now = over.pop("now", TS + 60)
    for name in ("bot_login", "public_key", "owner_id"):
        if name in over:
            trust[name] = over.pop(name)
    args.update(over)
    return proof.verify(
        proof.IssueView(**args),  # type: ignore[arg-type]
        proof.Trust(**trust),  # type: ignore[arg-type]
        now,  # type: ignore[arg-type]
    )


# --- the signed message and the test vector --------------------------------------------


def test_the_message_layout_is_pinned() -> None:
    got = proof.message(proof.Signed(7, "42", 100, "ab", "t", "x\ny"))

    def field(s: str) -> bytes:
        return struct.pack(">Q", len(s.encode())) + s.encode()

    want = b"aether-proof-v2" + b"".join(
        field(p) for p in ("BoxDelta/aether", "7", "42", "claude", "100", "ab", "t", "x\ny")
    )
    assert got == want


def test_length_prefixes_stop_fields_running_into_each_other() -> None:
    a = proof.message(proof.Signed(1, "2", 3, "n", "ab", "c"))
    b = proof.message(proof.Signed(1, "2", 3, "n", "a", "bc"))
    assert a != b


def test_the_test_vector_is_stable() -> None:
    assert body_for() == VECTOR_BODY


def test_the_public_pem_matches_the_key() -> None:
    pem = KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert pem.decode() == PUBLIC_PEM


def test_a_signed_issue_verifies() -> None:
    assert check() == proof.Result(True, "valid")


def test_unicode_title_and_text_round_trip() -> None:
    title, text = "Prüfe die Größe", "Ünïcödé — ok ✓"
    body = body_for(title=title, text=text)
    assert check(title=title, body=body).ok


# --- tamper, copy, wrong key, wrong person --------------------------------------------


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"title": TITLE + "!"}, "signature"),
        ({"title": " " + TITLE}, "signature"),
        ({"number": NUMBER + 1}, "signature"),
        ({"owner_id": "999"}, "signature"),
        ({"body": VECTOR_BODY.replace("probe", "prove")}, "signature"),
        ({"body": "extra line\n" + VECTOR_BODY}, "signature"),
        ({"public_key": Ed25519PrivateKey.generate().public_key()}, "signature"),
    ],
    ids=["title", "title-space", "number-copy", "owner", "body", "body-prefix", "other-key"],
)
def test_a_changed_input_fails(over: dict[str, object], reason: str) -> None:
    result = check(**over)
    assert not result.ok and reason in result.reason


def test_a_proof_copied_to_another_issue_fails() -> None:
    assert not check(number=NUMBER + 1).ok


def test_a_different_route_in_the_signature_is_not_accepted() -> None:
    forged = proof.message(signed(route="local"))
    sig = proof.proof_line(TS, NONCE, KEY.sign(forged))
    assert not check(body=f"{TEXT}\n{sig}").ok


def test_another_repository_in_the_signature_is_not_accepted() -> None:
    forged = proof.message(signed(repository="someone/else"))
    assert not check(body=f"{TEXT}\n{proof.proof_line(TS, NONCE, KEY.sign(forged))}").ok


@pytest.mark.parametrize("author", ["BoxDelta", "someone", "", "boxdelta-dispatch2"])
def test_only_the_bot_login_counts(author: str) -> None:
    result = check(author=author)
    assert not result.ok and "not opened by the bot" in result.reason


def test_a_pull_request_is_refused() -> None:
    result = check(is_pull_request=True)
    assert not result.ok and "pull request" in result.reason


# --- the proof line must be exact -----------------------------------------------------


def _line(body: str) -> str:
    return body.rpartition("\n")[2]


@pytest.mark.parametrize(
    "mangle",
    [
        lambda b: b + "\n",
        lambda b: b + " ",
        lambda b: b + "\ntrailing",
        lambda b: b.replace("\n<!--", "\r\n<!--"),
        lambda b: b.replace("\n<!--", "\n <!--"),
        lambda b: b.replace("v2", "v1"),
        lambda b: b.replace(" 1795900000 ", " 01795900000 "),
        lambda b: b.replace(NONCE, NONCE.upper()),
        lambda b: b.replace(NONCE, NONCE[:-1]),
        lambda b: b[:-5] + "-->",
        lambda b: b.rpartition("\n")[0],
        lambda b: _line(b),
        lambda b: "",
    ],
    ids=[
        "trailing-lf",
        "trailing-space",
        "text-after",
        "crlf",
        "indented",
        "version",
        "leading-zero",
        "upper-nonce",
        "short-nonce",
        "short-sig",
        "no-line",
        "line-only",
        "empty",
    ],
)
def test_a_malformed_proof_line_fails_closed(mangle: Callable[[str], str]) -> None:
    result = check(body=mangle(VECTOR_BODY))
    assert not result.ok


def test_a_none_body_fails() -> None:
    assert not check(body=None).ok


def test_a_non_canonical_signature_encoding_fails() -> None:
    # The last character of an 86-character signature carries 2 signature bits and 4 unused
    # bits. Q -> R sets only an unused bit: the same 64 bytes, a different text.
    sig = _line(VECTOR_BODY).split()[-2]
    assert sig.endswith("Q")
    other = sig[:-1] + "R"
    assert base64.urlsafe_b64decode(sig + "==") == base64.urlsafe_b64decode(other + "==")

    result = check(body=VECTOR_BODY.replace(sig, other))

    assert result == proof.Result(False, "no well-formed proof line at the end of the body")


def test_padding_is_not_part_of_the_signature_text() -> None:
    sig = _line(VECTOR_BODY).split()[-2]
    padded = sig + "=="
    assert not check(body=VECTOR_BODY.replace(sig, padded)).ok
    assert base64.urlsafe_b64decode(sig + "==")  # the unpadded one is the right shape


# --- time window ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "ok"),
    [
        (TS - 299, True),
        (TS - 300, True),
        (TS - 301, False),
        (TS, True),
        (TS + proof.MAX_AGE_SECONDS, True),
        (TS + proof.MAX_AGE_SECONDS + 1, False),
    ],
)
def test_the_time_window(now: float, ok: bool) -> None:
    assert check(now=now).ok is ok


# --- what the bot may sign ------------------------------------------------------------


def test_clean_input_passes() -> None:
    assert (
        proof.check_input("A title", "Some details\nsecond line\twith tab", ["a.py", "b/c.py"])
        is None
    )


@pytest.mark.parametrize(
    ("title", "details", "files"),
    [
        ("", "d", []),
        ("   ", "d", []),
        ("t" * (proof.MAX_TITLE + 1), "d", []),
        ("t", "d" * (proof.MAX_DETAILS + 1), []),
        ("t", "d", ["f"] * (proof.MAX_FILES + 1)),
        ("a <!-- b", "d", []),
        ("t", "x --> y", []),
        ("t", "see AETHER-PROOF v2", []),
        ("t", "d", ["x-->.py"]),
        ("t\r", "d", []),
        ("t", "d\r\ne", []),
        ("t\nu", "d", []),
        ("t", "d\x00", []),
        ("t", "d\x1b[0m", []),
        ("t", "d\x7f", []),
        ("t", "d\x85e", []),
        ("t", "d\u202ee", []),
        ("t", "d\u200be", []),
        ("t", "d\u2028e", []),
        ("t", "d\u2029e", []),
        ("t", "d\ud800e", []),
        ("t", "d\ue000e", []),
        ("t", "d\u0378e", []),
        ("t\u202e", "d", []),
        ("t", "d", ["a\u202e.py"]),
        ("t", "d", ["a\nb.py"]),
        ("t", "d", [" a.py"]),
        ("t", "d", [""]),
        ("t", "d", ["f" * (proof.MAX_FILE_NAME + 1)]),
    ],
)
def test_bad_input_is_refused_without_repeating_it(
    title: str, details: str, files: list[str]
) -> None:
    assert proof.check_input(title, details, files) is not None


def test_a_refusal_never_repeats_the_text() -> None:
    reason = proof.check_input("T", "SECRET-VALUE-42 <!-- more", ["SECRET-FILE.py"])
    assert reason is not None
    assert "SECRET" not in reason


def test_the_title_is_trimmed_before_signing() -> None:
    assert proof.normalize_title("  Fix it \n") == "Fix it"


def test_render_text_has_lf_endings_and_a_file_list() -> None:
    assert proof.render_text("  Do it.  ", ["a.py", "b.py"]) == "Do it.\n\nFiles:\n- a.py\n- b.py"
    assert proof.render_text("Do it.", []) == "Do it."


def test_a_rendered_and_signed_body_verifies() -> None:
    text = proof.render_text("Do it.", ["a.py"])
    subject = proof.Subject(5, "T", text)
    body = proof.sign_body(KEY, subject, OWNER, ts=TS, nonce=NONCE)
    assert body == f"{text}\n{_line(body)}"
    assert check(number=5, title="T", body=body).ok


def test_sign_body_defaults_use_a_fresh_nonce_and_the_clock() -> None:
    subject = proof.Subject(1, "T", "x")
    one = proof.sign_body(KEY, subject, OWNER)
    two = proof.sign_body(KEY, subject, OWNER)
    assert one != two
    assert check(number=1, title="T", body=one, now=time.time()).ok


# --- loading the public key and the owner id -------------------------------------------


def _root_like(tmp_path: Path) -> int:
    """Files in tmp_path belong to this user: stand in for root with this uid."""
    tmp_path.chmod(0o755)
    return os.getuid()


def test_the_default_paths_are_fixed() -> None:
    assert Path("/etc/aether-dispatch/proof.pub") == proof.PUBLIC_KEY_PATH
    assert Path("/etc/aether-dispatch/owner_id") == proof.OWNER_ID_PATH
    assert proof.REPOSITORY == "BoxDelta/aether"


def test_no_environment_variable_changes_where_the_key_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Path] = []

    def spy(path: Path, trusted_uid: int) -> str:
        seen.append(path)
        return "the file is missing, or is a symbolic link"

    monkeypatch.setattr(proof, "_read_trusted", spy)
    for name in ("DISPATCH_ENV_FILE", "DISPATCH_PROOF_PUBKEY", "PROOF_PUBLIC_KEY", "HOME"):
        monkeypatch.setenv(name, "/nonexistent/evil")

    proof.load_public_key()
    proof.load_owner_id()

    assert seen == [Path("/etc/aether-dispatch/proof.pub"), Path("/etc/aether-dispatch/owner_id")]


def test_the_module_never_reads_the_environment() -> None:
    source = Path(proof.__file__).read_text()
    assert "os.environ" not in source and "getenv" not in source


def test_a_trusted_public_key_loads(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text(PUBLIC_PEM)
    path.chmod(0o644)

    key = proof.load_public_key(path, trusted_uid=uid)

    assert isinstance(key, Ed25519PublicKey)
    assert check(public_key=key, now=TS + 1).ok


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_writable_key_file_is_refused(tmp_path: Path, mode: int) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text(PUBLIC_PEM)
    path.chmod(mode)
    result = proof.load_public_key(path, trusted_uid=uid)
    assert isinstance(result, str) and "root-owned" in result


def test_a_writable_folder_is_refused(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text(PUBLIC_PEM)
    path.chmod(0o644)
    tmp_path.chmod(0o775)
    try:
        result = proof.load_public_key(path, trusted_uid=uid)
    finally:
        tmp_path.chmod(0o755)
    assert isinstance(result, str) and "root-owned" in result


def test_a_file_owned_by_someone_else_is_refused(tmp_path: Path) -> None:
    _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text(PUBLIC_PEM)
    path.chmod(0o644)
    result = proof.load_public_key(path, trusted_uid=os.getuid() + 1)
    assert isinstance(result, str) and "root-owned" in result


def test_a_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    real = tmp_path / "real.pub"
    real.write_text(PUBLIC_PEM)
    real.chmod(0o644)
    link = tmp_path / "proof.pub"
    link.symlink_to(real)
    result = proof.load_public_key(link, trusted_uid=uid)
    assert isinstance(result, str) and "symbolic link" in result


def test_a_symlinked_folder_is_refused(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    (real / "proof.pub").write_text(PUBLIC_PEM)
    (real / "proof.pub").chmod(0o644)
    folder = tmp_path / "etc"
    folder.symlink_to(real)

    result = proof.load_public_key(folder / "proof.pub", trusted_uid=uid)

    assert isinstance(result, str)


def test_a_file_over_the_limit_is_refused_not_cut_off(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text(PUBLIC_PEM + "x" * 100_000)
    path.chmod(0o644)
    assert proof.load_public_key(path, trusted_uid=uid) == "public key: the file is too large"

    owner = tmp_path / "owner_id"
    owner.write_text("123" + " " * (proof.MAX_KEY_FILE_BYTES - 2) + "456")
    owner.chmod(0o644)
    assert proof.load_owner_id(owner, trusted_uid=uid) is None


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    os.mkfifo(path, 0o644)
    assert (
        proof.load_public_key(path, trusted_uid=uid) == "public key: the file is not a regular file"
    )


def test_a_wrong_folder_is_refused_before_the_file_is_opened(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    tmp_path.chmod(0o775)
    path = tmp_path / "proof.pub"
    os.mkfifo(path, 0o644)
    try:
        result = proof.load_public_key(path, trusted_uid=uid)
    finally:
        tmp_path.chmod(0o755)
    assert isinstance(result, str) and "root-owned" in result


def test_a_missing_key_file_is_a_reason_not_an_exception(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    result = proof.load_public_key(tmp_path / "nope.pub", trusted_uid=uid)
    assert result == "public key: the file is missing, or is a symbolic link"


def test_garbage_and_other_key_types_are_refused(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "proof.pub"
    path.write_text("not a key")
    path.chmod(0o644)
    assert proof.load_public_key(path, trusted_uid=uid) == "public key: cannot be read"

    other = ec.generate_private_key(ec.SECP256R1()).public_key()
    path.write_bytes(
        other.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    assert proof.load_public_key(path, trusted_uid=uid) == "public key: not an Ed25519 key"


def test_the_owner_id_loads_only_from_a_trusted_file(tmp_path: Path) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "owner_id"
    path.write_text(OWNER + "\n")
    path.chmod(0o644)
    assert proof.load_owner_id(path, trusted_uid=uid) == OWNER

    path.chmod(0o666)
    assert proof.load_owner_id(path, trusted_uid=uid) is None


@pytest.mark.parametrize("content", ["", "abc", "12 34", "-1", "1" * 21, "\uff11\uff12\uff13"])
def test_a_bad_owner_id_is_refused(tmp_path: Path, content: str) -> None:
    uid = _root_like(tmp_path)
    path = tmp_path / "owner_id"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    assert proof.load_owner_id(path, trusted_uid=uid) is None


# --- cryptography missing fails closed --------------------------------------------------


def test_a_failing_cryptography_import_is_none_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "cryptography.hazmat.primitives.asymmetric.ed25519",
        "cryptography.hazmat.primitives.asymmetric",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    assert proof._ed25519() is None
    assert check() == proof.Result(False, "cryptography is not installed")


def test_the_module_imports_without_cryptography() -> None:
    code = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "sys.modules['cryptography'] = None\n"
        "from dispatch import proof\n"
        "assert proof._ed25519() is None\n"
        "assert proof.load_public_key() == 'cryptography is not installed'\n"
        "print('ok')\n"
    )
    repo = Path(proof.__file__).resolve().parent.parent
    run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", "-c", code, str(repo)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (run.returncode, run.stdout.strip()) == (0, "ok"), run.stderr


def test_without_cryptography_nothing_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proof, "_ed25519", lambda: None)

    result = check()

    assert result == proof.Result(False, "cryptography is not installed")
    assert proof.load_public_key() == "cryptography is not installed"
