# ADR-039: One main chat with automatic topics

**Status:** Accepted\
**Date:** 2026-11-27\
**Owner:** BoxDelta

## Context

Phase 3 adds the chat view (docs/08 Step 4). A normal list of separate
chats gets cluttered fast. The owner wants one ongoing main chat with
Aether, with Aether moving subjects into topic chats ("Topics") on its own, so the main
chat stays clean and memory carries the long-term context.

## Options

1.  Separate chats the user creates and picks (the usual chat-app
    model). Rejected: clutter, and the user has to sort everything.
2.  One endless chat. Rejected: no structure; context grows without limit.
3.  One main chat plus topics that Aether creates and routes to
    (chosen).

## Decision

Option 3.

-   **Main chat** (the Chat tab in the UI): one per user, never closed. Older turns are summarised
    and dropped from the live context; durable facts reach Memory only
    through the normal approval flow (Phase 2).
-   **Topics:** each is tied to a topic (a Memory category or
    project). Aether creates one when a conversation turns into its own
    topic. Above the reply, the main chat shows a link: "New topic ·
    <topic>" with a "Use an existing one" option. Choosing an existing
    topic merges the new one into it.
-   **Routing:** a message typed in the main chat may be routed to a
    topic. The reply shows "Added to · <topic>" with a "Move" option.
    Messages typed inside a topic stay there. Routing runs on the
    local model through Nexus; when it is unsure, the message stays in
    the main chat. Routing never changes a message's privacy level.
-   **Archiving:** a topic with no messages for 14 days (setting) is
    archived. On archive, Aether proposes a short summary for Memory (the
    owner approves or rejects it). Archived topics can be reopened or
    merged.
-   **UI:** the Chat tab opens the main chat. "Topics" is its only
    sub-tab, indented under Chat and shown only while Chat or Topics is
    open. On the
    phone, topics open from the links in the main chat and from a
    button in the Chat header; the tab bar stays at five tabs.
-   **Privacy:** a topic's context is the highest privacy level of
    its messages; routing to providers follows docs/05 §7.1 as usual.
-   **Events:** `chat.topic.created`, `chat.message.routed`,
    `chat.topic.merged`, `chat.topic.archived`, each with the actor and
    the routing reason (docs/01 §7).

## Consequences

-   New `chats` table (the main chat and topics) in Postgres (ADR-011); messages
    carry a `chat_id`.
-   Wrong routing is the main risk: the link above every routed reply and
    the Move option keep it visible and easy to fix. Misroutes are logged
    so the routing rules can be tuned.
-   Adds work to Step 4 of the build order (Step 4b).

## Revisit when

-   Routing is wrong often enough to annoy daily use, or the owner wants
    topics created only after asking.
