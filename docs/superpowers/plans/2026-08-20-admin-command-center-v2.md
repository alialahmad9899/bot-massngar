# Admin Command Center V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing admin mode into structured, auditable, dynamic administration with schedules, change history, CRM/lead scoring, and analytics while preserving the current `app.py` behavior and Render 512MB constraints.

**Architecture:** Add a small runtime extension module that patches the existing app only after Gunicorn initializes it. Structured admin commands are parsed into validated tool calls; writes reuse the existing SQLite tables and add history/CRM/offer tables. Existing admin commands remain as a fallback. No Redis, Celery, extra workers, or external services are introduced.

**Tech Stack:** Python, SQLite/WAL, Flask/Gunicorn, existing Gemini/Facebook integrations.

**Spec:** Current repository review and approved implementation direction from the conversation.

## Global Constraints

- Keep Render memory target at 512MB.
- Keep current `app.py`, Gemini flow, Messenger Webhook, Human Handover, and existing admin commands working.
- Do not add a new data provider or external queue service.
- Structured admin tools must validate inputs before database writes.
- Sensitive destructive changes require existing confirmation flow or an equivalent explicit confirmation.
- Every admin write must be auditable and reversible where practical.

---

### Task 1: Add tests for structured commands, history, CRM, and offers

**Files:**
- Create: `test_admin_command_center_v2.py`

- [ ] **Step 1: Write failing tests** for command parsing, validation, course update history, offer scheduling, lead scoring, and rollback behavior.
- [ ] **Step 2: Run the focused tests** and confirm they fail because the runtime module does not yet exist.
- [ ] **Step 3: Implement the smallest test fixtures** with an isolated SQLite database and fake app module.
- [ ] **Step 4: Re-run the focused tests** after implementation and require all to pass.

### Task 2: Implement structured admin runtime extension

**Files:**
- Create: `admin_runtime.py`

- [ ] **Step 1:** Add schema migration for `academy_change_history`, `academy_offers`, and `customer_leads`.
- [ ] **Step 2:** Add deterministic parser returning `{tool, args, confirmation_required}` for course, batch, information, offer, rollback, and reporting commands.
- [ ] **Step 3:** Validate numbers, dates, names, and required fields before writes.
- [ ] **Step 4:** Implement tool execution against existing `academy_courses`, `course_batches`, and `academy_info` tables.
- [ ] **Step 5:** Record before/after snapshots for every admin write and support rollback of the latest course change.
- [ ] **Step 6:** Add lead upsert/scoring and admin lead-report queries.
- [ ] **Step 7:** Extend dynamic knowledge with active offers and relevant operational information.
- [ ] **Step 8:** Wrap existing `admin_execute` and `generate_ai_reply` without deleting the original behavior; unknown admin text falls back to the existing handler.

### Task 3: Load the runtime extension safely under Gunicorn

**Files:**
- Create: `gunicorn.conf.py`

- [ ] **Step 1:** Add a `post_worker_init` hook that imports `admin_runtime` and patches the loaded `app` module once.
- [ ] **Step 2:** Keep the current Render start command compatible with `app:app`.
- [ ] **Step 3:** Avoid starting additional worker processes or permanent background schedulers.

### Task 4: Verify and document

**Files:**
- Create: `ADMIN_COMMANDS_V2.md`

- [ ] **Step 1:** Run focused admin tests and Python compilation.
- [ ] **Step 2:** Run the existing repository admin/hardening tests where compatible.
- [ ] **Step 3:** Review the diff for accidental changes to existing files.
- [ ] **Step 4:** Commit the feature branch and publish only after verification.
