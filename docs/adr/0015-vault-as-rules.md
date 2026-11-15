# ADR-015: Vault starts as rules, not a service

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

A dedicated secrets service is heavy for a single-user v1.

## Decision

Secrets live only in a git-ignored .env (or OS keyring); .env.example lists names; Sentinel scans every push for secrets; logs redact known secret values.

## Consequences

Simple and safe enough for v1. No central audit of secret access.

## Revisit when

Hosted deployment, multiple machines, or shared credentials.
