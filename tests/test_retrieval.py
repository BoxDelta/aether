"""Memory selection and rendering (ADR-024 §3, step 11 of #58, #139). No database."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from core.identity import SECTION_HEADINGS, load_identity, render_system_prompt
from memory.retrieval import (
    MAX_CHARS,
    MAX_RECORDS,
    PREAMBLE,
    Selection,
    effective_privacy,
    render_line,
    select_memory,
)
from memory.store import Candidate, MemoryRecord, search_terms
from router.claude import ClaudeProvider
from router.contract import NON_SECRET_LEVELS, PrivacyLevel, ProviderInfo, privacy_rank
from router.ollama import OllamaProvider

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
DEFAULT = ("public", "personal")


def record(content: str = "canary fact", **changes: Any) -> MemoryRecord:
    values: dict[str, Any] = {
        "id": uuid4(),
        "content": content,
        "key": None,
        "category": "project",
        "privacy": "public",
        "status": "approved",
        "source": "owner",
        "source_provider": None,
        "source_request_id": None,
        "privacy_source": "owner",
        "created_at": NOW - timedelta(days=1),
        "updated_at": NOW - timedelta(days=1),
        "approved_at": NOW - timedelta(days=1),
        "expires_at": None,
    }
    return MemoryRecord(**{**values, **changes})


def cand(
    content: str = "canary fact", *, keyed: bool = False, rank: float = 0.0, **ch: Any
) -> Candidate:
    return Candidate(record(content, **ch), keyed, rank)


def select(candidates: list[Candidate], **kw: Any) -> Selection:
    args: dict[str, Any] = {
        "limit": "sensitive",
        "include_by_default": DEFAULT,
        "include_sensitive": False,
        "now": NOW,
    }
    return select_memory(candidates, **{**args, **kw})


# --- privacy -------------------------------------------------------------------------


def test_default_levels_only_without_include_sensitive() -> None:
    sel = select([cand("a"), cand("b", privacy="personal"), cand("c", privacy="sensitive")])

    assert sorted(r.content for r in sel.records) == ["a", "b"]
    assert sel.left_out_privacy == 1


def test_sensitive_needs_include_sensitive_even_if_listed_by_default() -> None:
    both = ("public", "personal", "sensitive")
    cands = [cand("s", privacy="sensitive")]

    assert select(cands, include_by_default=both).records == ()
    assert len(select(cands, include_by_default=both, include_sensitive=True).records) == 1


def test_include_sensitive_does_not_add_a_level_the_limit_forbids() -> None:
    cands = [cand("p", privacy="personal"), cand("s", privacy="sensitive")]

    sel = select(cands, limit="personal", include_sensitive=True)

    assert [r.privacy for r in sel.records] == ["personal"]


@pytest.mark.parametrize("limit", NON_SECRET_LEVELS)
def test_never_above_the_limit(limit: PrivacyLevel) -> None:
    cands = [cand(level, privacy=level) for level in NON_SECRET_LEVELS]

    sel = select(cands, limit=limit, include_by_default=NON_SECRET_LEVELS, include_sensitive=True)

    assert sorted(r.content for r in sel.records) == sorted(
        NON_SECRET_LEVELS[: NON_SECRET_LEVELS.index(limit) + 1]
    )


def test_no_limit_means_no_memory() -> None:
    sel = select([cand("a")], limit=None)

    assert sel.records == () and sel.context is None and sel.left_out_privacy == 1


def test_secret_is_never_selected_and_never_a_limit() -> None:
    # MemoryRecord can't hold secret, so build the bad record without validation.
    bad = MemoryRecord.model_construct(**{**record("x").model_dump(), "privacy": "secret"})

    sel = select([Candidate(bad, False, 0.0)], include_by_default=("secret", "public"))

    assert sel.records == () and sel.left_out_privacy == 1
    with pytest.raises(ValueError, match="never secret"):
        select([], limit="secret")


def test_pending_rejected_and_expired_are_left_out() -> None:
    cands = [
        cand("ok"),
        cand("pending", status="pending", approved_at=None),
        cand("expired", expires_at=NOW),
        cand("future", expires_at=NOW + timedelta(seconds=1)),
    ]

    sel = select(cands)

    assert sorted(r.content for r in sel.records) == ["future", "ok"]
    assert sel.left_out_unusable == 2


def test_duplicate_ids_count_once() -> None:
    one = record("dup")

    sel = select([Candidate(one, False, 1.0), Candidate(one, False, 0.5)])

    assert len(sel.records) == 1 and sel.left_out_unusable == 1


def test_bad_arguments() -> None:
    with pytest.raises(ValueError, match="timezone"):
        select([], now=datetime(2026, 1, 1))
    with pytest.raises(TypeError):
        select([], include_sensitive="yes")
    with pytest.raises(ValueError, match="lowered"):
        select([], max_records=MAX_RECORDS + 1)
    with pytest.raises(ValueError, match="lowered"):
        select([], max_chars=MAX_CHARS + 1)
    with pytest.raises(ValueError, match="lowered"):
        select([], max_records=-1)


# --- order and size ------------------------------------------------------------------


def test_order_keyed_then_rank_then_recent_then_id() -> None:
    old, new = NOW - timedelta(days=5), NOW - timedelta(days=1)
    low, high = UUID(int=1), UUID(int=2)
    cands = [
        cand("recent", updated_at=new),
        cand("old", updated_at=old),
        cand("ranked", rank=0.5, updated_at=old),
        cand("keyed", keyed=True, updated_at=old),
        cand("tie-high-id", id=high, updated_at=new + timedelta(hours=1)),
        cand("tie-low-id", id=low, updated_at=new + timedelta(hours=1)),
    ]

    sel = select(cands)

    assert [r.content for r in sel.records] == [
        "keyed",
        "ranked",
        "tie-low-id",
        "tie-high-id",
        "recent",
        "old",
    ]


def test_selection_is_deterministic() -> None:
    cands = [cand(f"c{i}", rank=i % 3) for i in range(8)]

    assert select(cands).record_ids == select(list(reversed(cands))).record_ids


def test_record_limit_drops_the_lowest() -> None:
    cands = [cand(f"c{i}", rank=float(i)) for i in range(MAX_RECORDS + 5)]

    sel = select(cands)

    assert len(sel.records) == MAX_RECORDS
    assert sel.left_out_size == 5
    assert sel.records[0].content == f"c{MAX_RECORDS + 4}"


def test_lower_limits_are_honoured() -> None:
    cands = [cand(f"c{i}", rank=float(i)) for i in range(5)]

    sel = select(cands, max_records=2)

    assert len(sel.records) == 2 and sel.left_out_size == 3
    assert select(cands, max_records=0).records == ()


def test_char_limit_drops_whole_records_and_a_lower_never_displaces_a_higher() -> None:
    big = cand("x" * 100, rank=2.0)
    small = cand("y", rank=1.0)
    line = len(render_line(big.record))

    sel = select([big, small], max_chars=line - 1)

    # big doesn't fit, so nothing below it may take its place.
    assert sel.records == () and sel.left_out_size == 2
    fits = select([big, small], max_chars=line)
    assert [r.content for r in fits.records] == ["x" * 100] and fits.left_out_size == 1
    assert fits.chars <= line


def test_total_never_exceeds_max_chars() -> None:
    cands = [cand("z" * 300, rank=float(i)) for i in range(20)]

    sel = select(cands)

    assert sel.chars <= MAX_CHARS and sel.left_out_size == 20 - len(sel.records)


# --- rendering -----------------------------------------------------------------------


def test_line_shows_category_and_level() -> None:
    line = render_line(record("Uses Helix", category="preference", privacy="personal"))

    assert line == "- (preference, personal) Uses Helix"


@pytest.mark.parametrize(
    "ch",
    [chr(c) for c in (0x0A, 0x0D, 0x00, 0x1B, 0x2028, 0x2029, 0x200B, 0x202E, 0xE000, 0x85)],
)
def test_unsafe_characters_cannot_add_lines_or_hide_text(ch: str) -> None:
    line = render_line(record(f"before{ch}after"))

    assert line.startswith("- (project, public) before")
    assert line.endswith("after")
    assert "\n" not in line and ch not in line


def test_whitespace_runs_collapse() -> None:
    assert render_line(record("a \n\n\t  b")).endswith(") a b")


@pytest.mark.parametrize("heading", SECTION_HEADINGS)
def test_headings_are_neutralised(heading: str) -> None:
    bare = heading.rstrip(":")
    attacks = [
        heading,
        heading.upper(),
        heading.lower(),
        f"x {heading} y",
        f"{bare} :",
        f"{bare}\u200b:",
        f"{bare}\uff1a",  # full-width colon
        f"{bare}\ufe55",  # small colon
        "".join(chr(ord(c) + 0xFEE0) if c.isalpha() else c for c in heading),  # full-width
    ]
    for attack in attacks:
        body = render_line(record(attack)).split(") ", 1)[1]
        assert not re.search(re.escape(bare) + r"\s*[:\uff1a]", body, re.IGNORECASE), body


def test_rendered_prompt_has_each_heading_once() -> None:
    attack = "\n\n".join(SECTION_HEADINGS)
    sel = select([cand(attack)])
    identity = load_identity()

    prompt = render_system_prompt(identity, memory_context=sel.context, task="do it")

    for heading in SECTION_HEADINGS:
        assert prompt.count(heading) <= 1, heading
    assert prompt.count("Relevant memory:") == 1
    assert prompt.count("Task instructions:") == 1


def test_context_starts_with_the_preamble_and_is_none_when_empty() -> None:
    sel = select([cand("fact")])

    assert sel.context is not None
    assert sel.context.splitlines()[0] == PREAMBLE
    assert sel.context.splitlines()[1:] == list(sel.lines)
    assert select([]).context is None


def test_selection_repr_has_no_content() -> None:
    sel = select([cand("canary-secret-ish-content-7c1e")])

    assert "canary-secret-ish-content-7c1e" not in repr(sel)
    assert "canary-secret-ish-content-7c1e" not in repr(sel.records)


def test_summary_properties() -> None:
    sel = select(
        [cand("a"), cand("b", privacy="personal"), cand("c", privacy="sensitive")],
        include_sensitive=True,
    )

    assert sel.count_by_level() == {"public": 1, "personal": 1, "sensitive": 1}
    assert sel.highest_level == "sensitive"
    assert select([]).highest_level is None
    assert len(sel.record_ids) == 3


@pytest.mark.parametrize(
    ("request_level", "memory_level", "expected"),
    [
        ("public", "personal", "personal"),
        ("personal", "public", "personal"),
        ("public", "sensitive", "sensitive"),
        ("sensitive", "public", "sensitive"),
        ("personal", "personal", "personal"),
    ],
)
def test_effective_privacy_is_the_higher(
    request_level: PrivacyLevel, memory_level: PrivacyLevel, expected: PrivacyLevel
) -> None:
    sel = select([cand("m", privacy=memory_level)], include_sensitive=True)

    assert effective_privacy(request_level, sel) == expected
    assert effective_privacy("public", select([])) == "public"


# --- search terms --------------------------------------------------------------------


def test_search_terms_matches_any_word() -> None:
    assert search_terms("What is my preferred editor?") == "what or is or my or preferred or editor"


@pytest.mark.parametrize("text", [None, "", "   ", "?!-- \"'", "or", "or OR Or"])
def test_search_terms_none_when_no_words(text: str | None) -> None:
    assert search_terms(text) is None


def test_search_terms_never_passes_operators() -> None:
    out = search_terms('a -b "c d" or e OR f') or ""

    assert out == "a or b or c or d or e or f"
    assert not any(ch in out for ch in '"-()&|!:<>')


def test_search_terms_dedupes_and_caps() -> None:
    assert search_terms("go go GO") == "go"
    many = " ".join(f"w{i}" for i in range(100))
    assert len((search_terms(many) or "").split(" or ")) == 32
    assert search_terms("a " + "x" * 65 + " b") == "a or b"


# --- provider memory limits (ADR-024 D3) ---------------------------------------------


def _info(**kw: Any) -> ProviderInfo:
    values: dict[str, Any] = {
        "name": "p",
        "kind": "local",
        "default_model": "m",
        "max_privacy": "sensitive",
    }
    return ProviderInfo(**{**values, **kw})


def test_memory_limit_defaults_to_none_so_a_new_provider_gets_no_memory() -> None:
    info = _info()

    assert info.memory_limit is None
    assert info.memory_limit_for(allow_cloud=True) is None
    assert info.memory_limit_for(allow_cloud=False) is None


def test_memory_limit_may_not_exceed_max_privacy() -> None:
    with pytest.raises(ValidationError, match="memory_limit"):
        _info(max_privacy="personal", memory_limit="sensitive")
    with pytest.raises(ValidationError):
        _info(memory_limit="secret")
    assert _info(max_privacy="personal", memory_limit="personal").memory_limit == "personal"


def test_cloud_memory_needs_allow_cloud() -> None:
    cloud = _info(kind="cloud", max_privacy="personal", memory_limit="public")

    assert cloud.memory_limit_for(allow_cloud=False) is None
    assert cloud.memory_limit_for(allow_cloud=True) == "public"


def test_local_memory_does_not_need_allow_cloud() -> None:
    local = _info(memory_limit="sensitive")

    assert local.memory_limit_for(allow_cloud=False) == "sensitive"


def test_unknown_kind_is_treated_as_cloud() -> None:
    info = _info(memory_limit="public").model_copy(update={"kind": "other"})

    assert info.memory_limit_for(allow_cloud=False) is None


def test_claude_provider_gets_personal_memory_at_most() -> None:
    # ADR-024 D3: personal after #8 (step 14); never sensitive.
    assert ClaudeProvider(None, "m").info.memory_limit == "personal"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("http://localhost:11434", "sensitive"),
        ("http://192.168.1.20:11434", "sensitive"),
        ("https://gpu-box.example.org", "public"),
        ("http://8.8.8.8:11434", "public"),
    ],
)
def test_ollama_memory_limit_follows_host_trust(host: str, expected: PrivacyLevel) -> None:
    assert OllamaProvider(host, "m").info.memory_limit == expected


def test_ollama_opt_in_trust_gives_sensitive_memory() -> None:
    p = OllamaProvider("http://100.64.0.1:11434", "m", trust_host=True)

    assert p.info.memory_limit == "sensitive"


# --- every combination, through the real provider limits (ADR-024 §8 exit 2) -------------


@pytest.mark.parametrize("allow_cloud", [False, True])
@pytest.mark.parametrize("include_sensitive", [False, True])
@pytest.mark.parametrize("status", ["pending", "approved", "expired"])
@pytest.mark.parametrize("level", NON_SECRET_LEVELS)
@pytest.mark.parametrize(
    ("name", "info", "ceiling"),
    [
        ("claude", ClaudeProvider(None, "m").info, "personal"),
        ("ollama-trusted", OllamaProvider("http://localhost:11434", "m").info, "sensitive"),
        ("ollama-untrusted", OllamaProvider("https://gpu.example.org", "m").info, "public"),
    ],
)
def test_provider_limit_into_selection(
    name: str,
    info: ProviderInfo,
    ceiling: PrivacyLevel,
    level: PrivacyLevel,
    status: str,
    include_sensitive: bool,
    allow_cloud: bool,
) -> None:
    changes: dict[str, Any] = {"privacy": level}
    if status == "pending":
        changes.update(status="pending", approved_at=None)
    if status == "expired":
        changes["expires_at"] = NOW - timedelta(seconds=1)
    limit = info.memory_limit_for(allow_cloud=allow_cloud)

    sel = select([cand("x", **changes)], limit=limit, include_sensitive=include_sensitive)

    expected = (
        status == "approved"
        and limit is not None
        and privacy_rank(level) <= privacy_rank(ceiling)
        and (level != "sensitive" or include_sensitive)
        and (info.kind == "local" or allow_cloud)
    )
    assert bool(sel.records) == expected, (name, level, status, include_sensitive, allow_cloud)
