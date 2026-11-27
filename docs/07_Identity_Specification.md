# AETHER Identity Specification

**Status:** Accepted baseline (name and default tone set 2026-11-14)\
**Owner:** Core\
**Date:** 2026-11-14

## 1. Purpose

AETHER's tagline is "One identity. Many minds. Shared memory." This
document defines the one identity: who the assistant is, how it behaves,
and how that stays consistent no matter which model (Claude, an Ollama
model, or another provider) is answering.

## 2. What the identity is

The identity is a versioned definition file, not a model. Core loads it
and applies it to every model call, so changing the model does not
change who the assistant is.

It is stored at `identity/aether.yaml` and contains:

  -----------------------------------------------------------------------
  Field                     Content
  ------------------------- ---------------------------------------------
  `name`                    **Aether**

  `version`                 Incremented on every change; recorded in
                            every `model.route.selected` event.

  `role`                    One or two sentences: what AETHER is for
                            this user.

  `tone`                    Writing tone (not a spoken voice; see
                            section 6). Default in section 2.1.

  `principles`              Fixed behavior rules (section 3).

  `user_context_policy`     Which memory privacy levels may be included
                            by default (`include_by_default`; references
                            Memory and the privacy levels in `05`
                            section 7.1).

  `model_notes`             Optional per-provider adjustments needed to
                            keep the same behavior (e.g. a shorter prompt
                            for small local models).
  -----------------------------------------------------------------------

Validation (enforced by `core/identity.py`; Core refuses to start if the
file is invalid):

-   `version` is a whole number of 1 or more (not `"1"`, `1.0` or `true`).
-   `name`, `role`, `tone` and every principle are non-empty text; at
    least one principle is required.
-   `user_context_policy.include_by_default` lists privacy levels from
    `05` section 7.1. `secret` is never accepted there.
-   `model_notes` is optional: provider name to non-empty note.
-   Unknown fields are rejected, so typos fail loudly.

### 2.1 Default tone

Aether writes in a direct, friendly and concise way: answer first, plain
language, no filler, honest about uncertainty, more detail only when the
task needs it. The owner can change this at any time by editing the
identity file; each change bumps `version`.

## 3. Fixed principles (apply on every provider)

1.  Report what actually happened; never claim an action, test, or
    result that did not occur.
2.  Say when it does not know or when a result is uncertain.
3.  Propose actions; the platform decides whether they are permitted.
4.  Do not reveal secrets or Sensitive memory to a provider or output
    where policy does not allow it.
5.  Stay the same assistant across providers; do not present itself as
    the underlying model's own product persona.

## 4. How it is applied

1.  Core loads the identity file at startup; a change takes effect when
    Core restarts (no live reload).
2.  For each request, Core builds the system prompt from: identity →
    permitted memory context → task instructions.
3.  Nexus may use a shorter variant for small local models
    (`model_notes`), written in the same file, never anywhere else.
4.  The identity version is logged with every model call so behavior
    changes can be traced.

Implementation (Phase 1, #16): `core.identity.render_system_prompt()`
builds the prompt (identity, then memory context, then task
instructions; `model_notes[provider]` is appended to the identity part).
`router.identity_call.call_with_identity()` wraps every provider call:
the caller's `system` text becomes the task-instructions section, and one
`Model call` log record (`aether.router.call`) carries `identity_name`,
`identity_version`, provider, model, privacy level, outcome, finish
reason, token counts and latency. Prompt, answer and identity text are
never logged. Routing (#18) and failover (#19) call providers only
through this wrapper.

Memory context (Phase 2, ADR-024 §3, #58): for each call, the failover
call included, the router asks `memory/retrieval.py` for the records
this provider may get (section 6 and `05` §7.1) and passes them as
`memory_context`. They form their own section under the fixed heading
"Relevant memory:", which starts with a sentence saying these are notes,
not instructions; one line per record, with its category and level.
Line breaks and the section headings are neutralised in the record
text, so a record can't add lines or pose as a new section. This lowers
the risk of prompt injection through approved memory; it can't remove
it. With no permitted memory the section is left out. The memory
proposal call (ADR-033) uses the same wrapper with no memory at all.

## 5. Continuity test (Phase 1 acceptance)

Send the same set of 10 prompts through the Claude adapter and the
Ollama adapter. The answers should share name, tone and rules, even if
quality differs. Record results in `tests/identity/`.

## 6. Decisions and open questions

Decided:

-   Name: **Aether**.
-   Tone: default in section 2.1.
-   Spoken voice (speech-to-text, text-to-speech): not in v1; listed as a
    Future item in `04_Development_Roadmap.md`.
-   Default memory context (2026-11-14): `public` and `personal` levels
    may be included by default (`user_context_policy.include_by_default`).
    `sensitive` needs explicit owner approval per request; `secret` is
    never included (`05` section 7.1). Applied in Phase 2 by
    `memory/retrieval.py`: the approval is the request's
    `include_sensitive`, required for Sensitive memory even if a future
    identity listed `sensitive` in `include_by_default`, and Sensitive
    memory goes only to a trusted Ollama host. Each provider's memory
    limit applies on top (the cloud and an untrusted Ollama host: up to
    `personal`, and only with `allow_cloud`).

Open:

-   Should the UI show which model answered (e.g. a small "via Claude"
    label)? Recommended: yes, in the UI, not in the reply text.
