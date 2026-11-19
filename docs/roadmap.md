# Product roadmap

This is the current product backlog. Checkboxes describe implementation, not deployment or operational acceptance. Existing verification reports remain historical evidence.

## Baseline and publication state

- [x] Reliability fixes for paper accounting and request retries are on `main` (`76e296b`). See [paper reliability](paper-reliability.md).
- [x] Persisted-evidence readiness reporting is implemented and committed (`fecee5d`). See [readiness](readiness.md).
- [x] Readiness PR #6 is merged on `main` (`6209b4e`); Python 3.11/3.14 CI passed.
- [ ] Review dependency PR #5 separately for compatibility and passing checks; it is not part of readiness acceptance.

Historical privacy remediation is owner-managed and separate from this product backlog. This plan does not authorize history rewrites, account changes, or removal of retained references.

## Verified offline paper lifecycle

Build on existing tests and contracts rather than introducing a competing framework. Keep the autopilot and elite paper books separate.

- [x] Add an [installed-wheel offline walkthrough](paper-walkthrough.md) proving wait, actual entry, monitor/exit, avoid inspection, and persisted accounting with synthetic inputs. Independent operational acceptance remains separate.
- Existing `test_paper_retry.py` covers restart/duplicate and concurrent-process entry/trim/close and caps; `test_paper_reliability.py` covers real SQLite failure rollback. Extend only uncovered scenarios below, not duplicate harnesses.

- [ ] Exercise ingestion, tape, analysis, ledger, effective decision, actual paper opening, monitoring, trim/exit, and outcome reporting through real CLI processes and temporary SQLite databases.
- [x] Cover rejection and position-cap refusal; returned decisions, persisted candidate state and events agree on `paper_wait`, with no position/fill created. See the real-run [sanitized case](examples/offline-paper-case.json) for evidence-to-outcome linkage and explicit missing forward-window samples.
- [ ] Cover duplicate, reordered, late, stale, unavailable, and wrong-mint evidence without using information unavailable at decision time.
- [ ] Exercise interrupted writes, restart, lock contention, concurrent invocations, and corrupt storage. Assert conservation and no duplicate accounting.
- [x] Verify clean installation using the built wheel and the offline walkthrough.
- [ ] Verify upgrade and restore using the built wheel, not only an editable checkout.
- [ ] Compare CLI, MCP, and job-wrapper semantics where supported; document unsupported cases explicitly.

Acceptance: reproducible offline replay, real persisted results, explicit invariant assertions, independent review, and a passing full verification run. Unit-test success alone is not operational acceptance.

## Test portability follow-through

- [x] Normalize expected temporary paths in corrupt-book and missing-roster CLI tests; canonical paths now match runtime resolution, including symlinked temporary roots.

## Next milestone: dependable paper operation

The offline journey is verified; real-data unattended operation is not. Prioritize these gaps before additional providers or strategy features. Reuse existing restart/concurrency coverage instead of adding a duplicate test framework.

1. Persist complete-tick receipts: distinguish a completed position-monitoring cycle from ingestion alone, partial failures, stale inputs, and missing evidence.
2. Verify upgrade, backup/restore, and rollback on installed packages with accounting reconciliation.
3. Define a separately approved bounded real-data paper trial with duration, sample coverage, resource limits, and failure criteria set before it starts.
4. Reconcile CLI, MCP, and job-wrapper behavior where their operations overlap.

A release is not operational acceptance or evidence of profitability. Private execution plans and task notes belong in ignored local directories, not this public backlog.

## Readiness follow-through

- [ ] Specify durable complete-tick receipts before claiming autopilot loop health. Current readiness explicitly reports this evidence as unsupported.
- [ ] Define versioned config/strategy identity and decision-to-position linkage before adding attribution schemas.
- [ ] Add migration and restore tests before any schema expansion.

Keep collection timestamps distinct from trade timestamps. Preserve conservative refusal of journal-bearing SQLite books and document consistent-snapshot requirements.

## Later: observation-only enrichment

Only after lifecycle acceptance:

- [ ] Implement default-off, cache-only RugCheck normalization and bounded lookup.
- [ ] Test missing, malformed, wrong-mint, stale, and unavailable observations. Cache reads must not renew freshness.
- [ ] Prove unchanged classifications, eligibility, sizing, decisions, and primary ledger/artifacts with differential tests.

No collector, credentials, standing schedule, live requests, or policy influence are included in this stage. Additional providers require a documented evidence gap, not just availability.

## Operational acceptance, separately approved

- [ ] Define a bounded paper-lane trial with predeclared duration and sample coverage.
- [ ] Verify installed versus active versions, freshness, monitoring success, storage failures, reconciliation, and overlapping runs.
- [ ] Exercise rollback and restore before declaring the runtime dependable.

Provider activation, costs, credentials, schedules, deployment, and releases require separate approval. No wallet connection, signing, routing, or fund movement is part of this roadmap.

## Branch hygiene

Retain feature branches while their PRs are unmerged. After a successful merge, verify the target contains the work before deleting its feature branch. Preserve recovery worktrees and historical local refs until their owner explicitly retires them; never push them as part of routine cleanup.
