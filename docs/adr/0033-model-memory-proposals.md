# ADR-033: How a model proposes memory (`propose_memory`)

**Status:** Proposed (2026-11-27). Implements ADR-024 §2 and D7 (accepted:
model proposals only when a request opts in); step 15 of #58.\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

ADR-024 §2 says that when a `/chat` request sets `propose_memory: true`,
"Core asks the model for proposals in a fixed, strict format and parses
them as data". Proposals are stored `pending` with `source="model"`, are
never retrieved, and only the owner can approve them. ADR-024 doesn't say
**how** Core asks, **what** the model may propose from, or **which level**
a proposal gets. Those choices decide what a proposal can leak or poison,
so they are recorded here.

## Options

-   **A. A second, separate call** after the answer: same provider, a fixed
    instruction, and the latest user message as data. The model answers
    only with JSON. (Recommended.)
-   **B. In the same call:** add an instruction asking the model to append a
    proposals block to its answer, which Core strips before replying.
-   **C. Core extracts facts itself** (rules or a local model only).

Trade-offs:

-   B costs nothing extra, but mixes data into the answer. If stripping
    fails, the block reaches the owner's reply. Small local models
    (`llama3.1:8b`) often break such formats. The answer also contains
    memory text and the model's own claims, which would then be proposed
    back as "facts".
-   A doubles the calls for an opted-in request (latency and, on Claude,
    cost), but the answer is untouched and the proposal text is cleanly
    separated and parsed as a whole.
-   C sends nothing extra anywhere, but rules can't recognise facts, and a
    fixed local model ignores the provider the owner chose.

## Decision (Option A)

1.  **Opt-in per request.** `propose_memory` defaults to `false`. Nothing
    below happens without it.
2.  **After a successful answer only.** It doesn't happen if the answer
    failed, was a refusal, or was cut off (`finish_reason` not `stop`).
3.  **Same provider, same rules.** The proposal call goes to the provider
    that answered, through `call_with_identity` (identity, the #8
    tripwire, the echo scrub, `max_privacy`). It needs the same
    `allow_cloud`; nothing new leaves the machine, because the provider
    already received this text. **No memory is sent with it.**
4.  **From the latest user message only.** Not the answer, not earlier
    turns and not memory. So a proposal can only restate what the owner
    just wrote. The model's own claims and retrieved memory can't come
    back as new "facts".
5.  **Strict format.** The model returns only
    `{"proposals": [{"content": "...", "category": "...", "privacy": "..."}]}`,
    optionally inside one Markdown code fence. Anything else counts as one
    dropped item. Each item must have exactly those three string fields, a
    known category, and a level other than `secret`. At most 3 per
    request; the rest are dropped. Duplicates are dropped.
6.  **Level floor.** The stored suggested level is the higher of the
    model's suggestion and the request's privacy level (Personal by
    default). A model can't make the owner's text look more public than
    the owner's request said it was. A `secret` suggestion is dropped,
    never stored.
7.  **The store's rules apply unchanged.** Records are `pending`,
    `source="model"`, `source_provider` (the provider) and
    `source_request_id` (the request ID), and `privacy_source="suggested"`.
    The content length limit, the secret-shaped content check (`SecretCheck`)
    and the 50-pending limit drop what doesn't fit. Each stored proposal
    writes its `memory.proposed` event in the same transaction (step 5).
8.  **Never fails the answer.** A proposal call error, timeout, unparsable
    reply or store error means 0 proposals. One metadata-only log line
    gives the counts (proposed, dropped) and an error type, never text.
    The proposal call gets its own `model.call.completed` event, or a
    `provider.unavailable` event on failure, so cost and latency stay
    visible. No failover: proposals are optional.
9.  **The reply says how many.** `ChatResponse.memory_proposals` is the
    number stored as pending, or `null` when not requested. The owner
    reviews them with `GET /memory?status=pending`.
10. **Bounded.** The proposal call uses `temperature` 0, at most 512
    output tokens, and a 30 s timeout.

## Consequences

-   An opted-in request makes two model calls, roughly doubling its latency.
    On Claude it also pays for one more short call.
-   Phase 2 exit 1's wording (a fact saved while one provider handled a
    request is recalled by the other) can now be shown end to end. Saving
    still needs the owner's approval, as ADR-024 requires.
-   Poisoning is limited to what the owner typed in that message, and the
    owner approves every record with a stated level.
-   Limit: a model can still phrase a proposal wrongly or merge two facts.
    The owner can correct content, category and key when approving.
-   Limit: secret-shaped content in a format `SecretCheck` doesn't know can
    be proposed. It stays pending until the owner rejects it, and is never
    retrieved.
