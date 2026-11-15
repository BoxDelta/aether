# ADR-018: Sentinel v1 in GitHub Actions

**Status:** Accepted\
**Date:** 2026-11-14\
**Owner:** BoxDelta

## Context

Sentinel must be independent from the code author, including AI coding assistants.

## Decision

Sentinel v1 runs in GitHub Actions: gitleaks secret scan from Phase 0; tests, lint and type checks from Phase 1. Branch protection and CODEOWNERS on .github/ and CLAUDE.md enforce independence. AI review is a Future layer.

## Consequences

Independence enforced by GitHub permissions. Depends on GitHub Actions availability.

### Dependency layer: Dependabot (2026-11-14)

Sentinel's dependency and supply-chain layer is Dependabot: alerts and
security updates (repository settings) plus weekly version-update PRs
for `uv` and GitHub Actions from `.github/dependabot.yml`, with a 7-day
cooldown. Its PRs go through Sentinel and `safemerge` like any other PR.
Details in `05` section 10.2.

### Known limitation: not enforced on GitHub Free (2026-11-14)

The repository is private on GitHub Free, where branch protection and
rulesets are **not enforced** for private repositories. Consequences:

- Sentinel checks run on every push and pull request, but GitHub does
  not block a merge when they fail.
- CODEOWNERS on `.github/` and `CLAUDE.md` is not enforced either, since
  enforcing it requires branch protection or a ruleset.
- AI coding agents use the owner's GitHub credentials, so "agents open
  PRs, the owner merges" is a rule in `CLAUDE.md`, not a permission.

Accepted mitigation: the owner stays on GitHub Free and merges with a
local `safemerge` fish function that waits for Sentinel checks to pass.
Agents never merge their own PRs or push to `main` (`CLAUDE.md` rule 5).
Tracked in #10, closed as limited by plan.

### After the public flip (2026-11-14, ADR-022)

This amends the Decision above for CODEOWNERS. Once the repository is
public, independence comes from the rulesets on
`main` (ADR-022, "Ruleset design"):

- pull requests are required;
- the Sentinel status checks `checks` and `secrets` are required;
- force pushes and branch deletion are blocked;
- the bypass list is empty.

CODEOWNERS stays in the repository to route review requests, but it is
**not enforced**: no code-owner review is required, because there is one
maintainer and agent PRs are authored under the owner's account. The
owner reviews changes to the paths in `.github/CODEOWNERS` manually
before merging. Agents still use the owner's credentials, so "agents
don't merge or change settings" remains a rule, and `safemerge` stays
the merge path (`05` section 10.1).

## Revisit when

AI review is added, CI needs to run locally, the repository becomes
public, or the plan changes to one that enforces rulesets on private
repositories (then enable the ruleset in #10).
