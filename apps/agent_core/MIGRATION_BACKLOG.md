
# agent_core → ORSIM Migration Backlog (2026)

This document tracks the migration and refactor of agent_core to a standardized, reusable, and domain-agnostic agent-app-manager pattern, extracting core logic to the ORSIM library. Each step is actionable and validated for behavior parity.

---

## Phase 1: ORSIM Core Extraction & Standardization

- [ ] **1.1 Extract BaseAgent, BaseApp, BaseManager to ORSIM**
  - Move domain-agnostic agent, app, and manager logic to `orsim.agent_core`
  - Define clear interfaces and hooks for domain-specific extension
  - **Validation:** All agents can inherit from BaseAgent; no loss of functionality

- [ ] **1.2 Refactor one agent as pilot (e.g., DriverAgentIndie)**
  - Inherit from BaseAgent, use BaseApp/Manager where possible
  - Minimize domain logic in agent; maximize reuse
  - **Validation:** No regression in agent tests or simulation

---

## Phase 2: Incremental Domain Refactor

- [ ] **2.1 Refactor remaining agents (Assignment, Analytics, Passenger)**
  - Migrate to new base classes and interfaces
  - Extract any further reusable logic to ORSIM as discovered
  - **Validation:** All tests pass, behavior matches pre-refactor

- [ ] **2.2 Refactor App and Manager classes**
  - Inherit from BaseApp/BaseManager, unify lifecycle and orchestration logic
  - Remove duplicated code, use mixins for optional features (e.g., message queue)
  - **Validation:** No regression in app/manager tests

---

## Phase 3: Test, Document, and Clean Up

- [ ] **3.1 Update and expand tests for all refactored agents/apps/managers**
  - Add/maintain equivalence and integration tests
  - Ensure coverage for new base classes and interfaces

- [ ] **3.2 Document new design paradigm**
  - Update migration guides, ADRs, and code comments
  - Provide concrete usage examples for new pattern

- [ ] **3.3 Remove deprecated and legacy code**
  - Only after full validation and sign-off

---

## General Guardrails
- [ ] Validate after each step: all tests pass, no regression in simulation
- [ ] Track blockers, risks, and lessons learned inline as checklist notes
- [ ] Prioritize code reuse, type safety, and clear separation of domain/core logic

---

**Blockers, notes, and validation results should be added as checklist items are worked.**
