# AETHER --- Project Documentation

**Working tagline:** One identity. Many minds. Shared memory.\
**Document set:** Initial engineering baseline\
**Status:** Planning / pre-implementation\
**Last updated:** 2026-11-14 (scope revision, ADR-009 to ADR-018)

## What is AETHER?

AETHER is a cross-platform, multi-model personal AI platform built
around one persistent identity, shared memory, intelligent model
routing, controlled automation, and a unified user experience.

AETHER is intended to connect local and cloud AI models, coordinate
tools and agents, retain user-approved context across providers, and
make its work observable through a central interface.

AETHER is a software platform, not a new operating system. Its initial
host environment is Arch / GNOME, with Android access planned.

## Documentation map

  ----------------------------------------------------------------------------------
  Document                                       Purpose
  ---------------------------------------------- -----------------------------------
  `00_Project_Charter.md`                        Vision, goals, scope, principles,
                                                 and success criteria

  `01_Technical_Architecture_Specification.md`   System architecture, components,
                                                 data flows, and interfaces

  `02_Subsystem_Reference.md`                    Responsibilities and boundaries of
                                                 each named subsystem

  `03_Technology_Stack_and_Decisions.md`         Candidate technologies and decision
                                                 status

  `04_Development_Roadmap.md`                    Phased implementation plan and
                                                 milestones

  `05_Security_and_Permissions.md`               Security principles, permissions,
                                                 approvals, and recovery

  `06_Developer_Handoff.md`                      Guidance for developers and AI
                                                 coding assistants

  `07_Identity_Specification.md`                 What the single AETHER identity
                                                 is and how it is applied

  `CODE_TOUR.md`                                 One-page tour of the code for a
                                                 new developer

  `adr/`                                         Architecture decision records

  `runbooks/`                                    Step-by-step checks on the real PC
                                                 (Phase 1 exit check, #23)
  ----------------------------------------------------------------------------------

## Issue references

References like `#NN` in these docs and in code comments point to issues
and pull requests in the original private development repository (now
archived as `aether-private-archive`), not to this repository.

## Important status note

This documentation describes a proposed system. It does not mean the
components have already been installed, integrated, tested, or
implemented. Technology choices marked **Candidate** must be validated
against current documentation, compatibility, licensing, privacy terms,
and project requirements before adoption.

## Initial hardware context

-   Host OS: Arch / GNOME
-   GPU: NVIDIA, 8 GB VRAM
-   Storage: 1 TB
-   Optional future hardware: an older 6 GB GPU (not part of the
    initial baseline)

The first implementation should target the single 8 GB VRAM GPU only. Multi-GPU
support is explicitly deferred.

## v1 scope at a glance

v1 = Core + Nexus (Claude and Ollama) + Memory + a web UI that works on a
phone, with Sentinel checks in GitHub Actions from day one. Everything
else is on the Future list in `04_Development_Roadmap.md`.

## How to use these documents

Treat the Project Charter as the product-level source of truth and the
Technical Architecture Specification as the engineering baseline. Record
major changes as architecture decisions rather than silently changing
assumptions. Update the documents when implementation reveals a better
design.
