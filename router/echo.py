"""The echo backstop (docs/01 §7, ADR-024 §5): text a call sent must not come back
in an error message that is logged, stored or shown.

Adapters build ``ProviderError.message`` from safe parts only. This is the backstop
for one that doesn't: a message that contains any text sent in the call (a
message, the system prompt, a memory record's content or its rendered line) is
replaced as a whole. Texts shorter than ``MIN_ECHO_LENGTH`` are skipped: blanking
every message that contains "hi" would destroy ordinary errors.

``core/events.py`` applies the same check to stored events.
"""

from collections.abc import Iterable, Sequence

from router.contract import ModelRequest

MIN_ECHO_LENGTH = 8
REMOVED_ECHO = "[REMOVED: contained request text]"


def request_texts(request: ModelRequest) -> list[str]:
    """The request's own text, for the echo backstop (never stored)."""
    texts = [m.content for m in request.messages]
    if request.system:
        texts.append(request.system)
    return long_enough(texts)


def long_enough(texts: Iterable[str]) -> list[str]:
    return [t for t in texts if len(t) >= MIN_ECHO_LENGTH]


def echo_free(text: str, forbidden: Sequence[str]) -> str:
    """``text``, or ``REMOVED_ECHO`` if it contains one of ``forbidden``."""
    if any(f in text for f in forbidden if len(f) >= MIN_ECHO_LENGTH):
        return REMOVED_ECHO
    return text
