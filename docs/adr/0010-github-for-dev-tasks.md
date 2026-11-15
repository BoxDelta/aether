# ADR-010: GitHub Issues/Projects for development tasks

**Status:** Accepted (supersedes ADR-006)\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Plane was the candidate tracker. GitHub is already the repository host, and Plane would be another self-hosted service to run and synchronize.

## Decision

Track AETHER's development in GitHub Issues and a GitHub Project board. Runtime tasks AETHER performs for the user stay in AETHER's own database.

## Consequences

No extra service; Claude Code and Sentinel already integrate with GitHub. Fewer board features than Plane.

## Revisit when

GitHub Projects cannot express a needed workflow.
