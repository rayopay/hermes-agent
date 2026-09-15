# RP-HERMES-002: Meaningful block recurrence and orchestrator recovery

## Status and scope

Tracking PR: [rayopay/hermes-agent#6](https://github.com/rayopay/hermes-agent/pull/6) (ready for review; implementation and verification in progress, not merge/deployment ready).

**Behaviour approved; incremental implementation in progress.** This document formalises the agreed RP-HERMES-002 behaviour. The first counter-protection increment has focused test and independent review evidence below; the complete recovery feature, integration, merge, deployment and live-card recovery remain outstanding.

Develop in a dedicated branch of the maintained Hermes fork. Do not modify the installed runtime, live boards, profile permissions, product workspaces or running workers. Publication, release and live-card disposition are separate actions. Product orchestration remains with its owning PM; Hermes maintenance remains with Default.

## Plain-English goal

Cards should not be trapped in triage merely because different problems have the same broad category. The owning orchestrator should be able to move a card out of triage, either on a user's instruction or after independently verifying that recovery is safe. The system must still stop repeated attempts at an unresolved problem.

A retry means “try the same problem again.” A resolution means “the problem was addressed; here is what changed.” Those must not be interchangeable.

## Verified baseline

Fork base: `d77d61287012a53fe915c11e950bbcc72a0a7630`.

Upstream source inspected: `416a8177c25d87aa9929dfcf31f7964137d7fcdd`.

At these inspected sources, `hermes_cli/kanban_db.py::_route_block` compares `prev_kind == kind`. It does not identify the actual blocker. `BLOCK_RECURRENCE_LIMIT = 2` sends a second consecutive same-category report to triage. `unblock_task` and status-only `specify_triage_task` preserve recurrence. The orchestrator's existing `kanban_unblock` surface does not provide a triage exit. Changing category can also restart the count without proving resolution.

The counter is separate from dispatcher spawn/crash failures. Dependency waiting and review routing have their own semantics.

Upstream overlap inspected before design:

- [PR #69692](https://github.com/NousResearch/hermes-agent/pull/69692), open when inspected: audited manual triage promotion. This overlaps recovery but does not by itself establish the complete blocker-identity and agent-recovery contract below.
- [PR #107742](https://github.com/NousResearch/hermes-agent/pull/107742), open when inspected: supervisor completion of triage cards. This overlaps finalization, not the complete recurrence/recovery design.

Review actual diffs and tests before reusing either change. Preserve upstream authorship for any reused implementation. An open PR description is not a verified solution. Recheck upstream before integration and release.

## Approved decisions

### D1. Count unresolved problems, not category or wording

Keep three concepts separate: category, human-readable explanation and stable blocker identity.

- Distinct verified blockers in the same category must not automatically share a recurrence count.
- Rewording an unresolved blocker, changing its category, or supplying a new arbitrary identifier must not automatically reset protection.
- Where the failing native operation supplies reliable structured identity, use it rather than prose.
- For ambiguous agent reports, preserve a conservative unresolved identity until an authorised orchestrator classifies the report with a recorded explanation and evidence.
- Do not put an LLM similarity judgement or free-text hash on the safety-critical dispatch path.
- Alternating between unresolved blockers must not erase the count of either blocker.

This does not promise that software can infer real-world root causes perfectly. The design must distinguish runtime-enforced identity rules from the orchestrator's evidence assessment.

### D2. Orchestrators can recover triage cards

Expose a supported native recovery operation to authorised top-level orchestrator agents, not only the dashboard or a human shell operator. Prefer extending the existing Kanban surface over a new always-present core tool.

Two initiation modes are approved:

1. **User-directed:** the user instructs the owning orchestrator to recover the exact card. Record the instruction reference and recovery basis; instructions do not waive ownership, active-run, dependency or completion checks.
2. **Autonomous:** the owning orchestrator inspects the blocker and records evidence supporting safe recovery. It need not ask the user merely because the card is in triage.

The worker that became blocked may propose a remedy and supply evidence. It must not give itself a fresh recovery budget. Preserve delegated-child and worker scoping through tool, CLI and database entry points. Do not accept a caller-supplied author/profile string as proof of authority.

Use existing board/profile authorisation boundaries wherever possible. Product-role routing is an operational obligation, not a claim that the existing board has project-level RBAC. Any new enforcement needed must be described and tested explicitly; do not silently expand profile access.

### D3. Retry and resolved-resume are different operations

- **Retry** retains the unresolved blocker identity and recurrence protection. It does not claim that the cause was fixed.
- **Resolved-resume** acknowledges a specific blocker, explains what changed and references supporting evidence. It starts a new recovery cycle for that resolved blocker while preserving previous cycles and failures.
- Recovery targets an exact observed blocker/event version. Stale, duplicate or concurrent recovery must not reset a newer blocker or create multiple resumptions.
- A status drag, ordinary unblock, specification edit or category change is not a resolution acknowledgement.
- Reusing old recovery evidence must not provide an unlimited automatic retry loop. Define and test evidence/event freshness and escalation semantics before implementation is accepted.

No blanket board reset, counter-history deletion, replacement-card creation or unrestricted force-resume path.

### D4. Resume the correct phase without replaying accepted work

Preserve the card, original contract, branch/PR association, proof references, review decisions and historical side effects. Restore the correct remaining phase through native lifecycle machinery; unresolved dependencies still gate eligibility.

Examples:

- Environment startup failed, then the environment was repaired: resume the unfinished execution.
- Test execution was accepted but evidence delivery failed: recover delivery/finalization, not the original testing phase.
- The original work is fully accepted but the card is in triage: use an acceptance-preserving finalization path, never a temporary Ready transition that can launch the old execution.

Recovery itself does not mean completion. Any finalization capability must pass the existing shared completion/acceptance boundary, with required evidence and ownership revalidated. If safe phase-specific continuation cannot be represented natively, retain a non-dispatchable hold and report that gap; do not disguise it as completed implementation.

### D5. Recovery does not grant concurrent ownership

Reject recovery when a current claim, open run, live owning worker or unresolved process/runtime ownership makes resumption unsafe. Use existing process-identity and lifecycle helpers; PID absence or a Git retention lock alone is insufficient proof of safety.

Status, blocker acknowledgement, counter-cycle change and audit event must be transactionally consistent. Preserve dependency/review gates and avoid board-wide incidental releases. A successful recovery response must distinguish held, eligible and actually running states.

### D6. Keep an auditable explanation

Record the exact task and blocker/cycle, observed version, initiating actor/context, user-directed or autonomous basis, rationale, evidence references, prior/new state and timestamp using existing event infrastructure wherever adequate.

Evidence references must not contain secrets, credentials or private payload dumps. The audit records what was asserted and by whom; it does not magically prove an external fix. Tool responses and notifications must explain remaining holds and never promise a worker is running without a verified run.

### D7. Preserve old cards safely

Do not guess precise blocker identity from historical category-only counters. Treat unresolved legacy evidence conservatively until an explicit audited classification or recovery. Preserve all old events, runs, contracts and accepted evidence. No automatic triage sweep or recovery on upgrade.

Prefer additive, minimal state. Select event payloads versus schema additions only after tracing native readers, migrations and rollback behaviour. An old runtime must not silently bypass new holds; document any incompatibility and require controlled rollback.

## Acceptance scenarios

1. Two independently established capability blockers do not falsely escalate as the same blocker.
2. The same unresolved blocker still escalates when reworded or recategorised; alternating identities cannot erase outstanding counts.
3. An orchestrator can recover the exact triage card through the agent tool surface on a user's instruction, with audit evidence.
4. The orchestrator can autonomously recover after a verified remedy, with a recorded rationale and evidence, without a human drag.
5. Workers, delegated children and spoofed caller metadata cannot self-authorise recovery or reset counters.
6. A retry preserves recurrence; resolved-resume starts only the acknowledged blocker's new cycle and retains history.
7. Stale observations, duplicate requests and competing recovery/claim attempts do not release a newer hold or produce duplicate current workers.
8. Review-phase and dependency-gated cards retain their routing. Recovery cannot silently unblock unrelated cards.
9. Accepted work can be finalised or continued narrowly without replaying accepted execution or weakening completion checks.
10. Active/ambiguous ownership blocks recovery; tests distinguish database claim safety from real process/runtime safety.
11. Legacy unresolved cards keep conservative protection until an explicit audited decision. No board reset occurs.
12. Tool, CLI and supported dashboard transitions agree at the shared lifecycle boundary; ordinary status changes do not act as a hidden reset.
13. Diagnostics say whether a card is held, eligible or actually running and expose the recurrence/recovery reason accurately.
14. Existing dispatcher failure budgets, dependency waits, PR acceptance, review and interrupted-publication holds retain their separate protections.

## Implementation and verification approach

1. Trace native routing, all lifecycle write entry points, identity checks, process ownership, event consumers, completion and migration paths. Produce an implementation plan with exact files and APIs. Resolve technical questions below before executable changes.
2. Add real disposable-board regression tests demonstrating the failure before the fix. Use native imports and supported call paths, not AST-only substitutes or source-text assertions.
3. Implement the smallest shared lifecycle change and expose it through the orchestrator surface. Keep implementation in a topical module rather than growing the database facade substantially.
4. Exercise tool registry, CLI and applicable dashboard paths against isolated state, including denied-worker/delegated contexts and separate-connection races.
5. Review specification compliance first, then code quality/security/concurrency. Repair findings and re-run causal tests.
6. Run focused and broader Kanban regressions through `scripts/run_tests.sh`, with credential-free scratch HOME and worktree-local dependency state. Bound resource use and preserve logs. Report unavailable dependencies, baseline failures, mocks and unexecuted paths honestly.
7. Test interaction with RP-HERMES-001 and RP-HERMES-003 in a separate integration candidate once appropriate; they are separate open changes, not implicitly included in this branch.
8. Publish/merge/deploy only under the applicable separate approval and release process. Deployment requires exact backups, drained owners, isolated migration rehearsal, rollback plan and live verification.

## Selected incremental implementation direction

Technical review rejected a task-wide autonomous-recovery cap and a new mandatory human-renewal system: neither is necessary to implement the agreed blocker-specific protection. Keep the existing recurrence threshold per unresolved blocker/cycle. Retry preserves the count and does not override an exhausted cycle; an evidenced resolution can begin the acknowledged blocker's next cycle. No additional above-threshold retry allowance is introduced by default. Real subsequent fixes may qualify without an arbitrary task-lifetime ceiling.

The runtime checks exact identity, event order, stale observations and consumed evidence references. The trusted owning orchestrator assesses whether an external remedy is substantively new and relevant. This is not a proof that software can detect every false assertion or place an absolute bound on dishonest trusted-owner decisions.

Implement stable identities and conservative classification in a topical native module before enabling release. Use an additive per-task projection, revision and append-only event history; categories remain descriptive. Classification alone does not resume a card. Follow with exact-event recovery through the existing orchestrator tool/native boundary and supported runtime-settlement checks, then shared no-Ready finalization of accepted work. Each slice requires native tests and sequential specification/quality reviews.

Mixed old/new writers are not an approved deployment mode. Release and rollback must drain writers and preserve compatible code/state backups; generic mixed-binary trigger infrastructure is not implicitly part of this feature. Unknown process ownership or accepted-work phase remains a hold, not a reason to bypass existing protections.

## First implementation increment: verified evidence

Base of this increment: `de3b208a9948e2b3e4ed67120bd06d0f1bc11b75`; feature branch base remains `d77d61287012a53fe915c11e950bbcc72a0a7630`.

The only executable change in this increment makes `_route_block` increment the preserved count for all non-dependency, unclassified reports. The dependency branch is unchanged. This prevents category changes and dependency interludes from erasing unresolved recurrence; **it does not yet distinguish established blockers or enable orchestrator triage recovery**. It must not be deployed on its own as the solution to repeated triage.

Canonical `scripts/run_tests.sh`, isolated credential-free HOME, real disposable SQLite lifecycle:

- Unchanged-production causal red: **5 assertion failures, 1 pass**, exit 1; no setup/collection errors.
- Candidate focused green: **6 passed**, exit 0.
- Eight-file related suite: **61 passed**, exit 0, including those six cases.
- Independent specification and code-quality reviews approved this bounded increment only; full-feature acceptance remains outstanding.

The new tests use native create/claim/block/unblock/link/complete/readiness paths, not real application workers or external provider effects. Source SHA-256 for the tested `hermes_cli/kanban_db.py`: `a10824b99f6ad38b3d89766f02488b27afa1e40124dfb199fda52215f78ed869`. New test-file SHA-256: `4cbc8af8a849d47a64686b6d192e8da0d2a90ef74f028b4c895d1a21a98bd3de`.

Environment: Python 3.12.3, pytest 9.1.1, 85 distributions from unchanged existing core/dev lock selections. A `--locked` preparation attempt refused stale option metadata; an explicitly reviewed `--frozen` existing-lock path was used after verifying the selected direct requirements match. This is not lock-freshness qualification or a lock repair. The original install exit was lost when the private monitoring wrapper failed; independent offline consistency, unchanged frozen-sync dry-run, genuine library imports and identical pre/post distribution metadata established the resulting environment without repeating installation. Generated bytecode is not claimed unchanged. These limits remain part of the evidence.

No full-suite, real-worker/process-tree, dashboard application, migration, external acceptance, merge, deployment or live-card recovery proof is claimed by this increment.

## Technical questions to settle during design review

These are implementation details, not additional user policy decisions:

- Minimal stable blocker identity and per-cycle storage; native versus orchestrator-classified causes; conservative legacy fallback.
- Exact native recovery API and how existing tool/CLI/dashboard calls share it.
- Trusted actor derivation and existing board-access enforcement at every entry point.
- Freshness and finite autonomous recovery rules that prevent relabelling or recycled evidence from defeating the breaker.
- Phase-aware continuation/finalization without dispatching completed work.
- Transactional stale-event/claim fencing and reliable process/runtime quiescence checks.
- Compatibility with historical triage specification, notifier events, schema upgrades, rollback and the other downstream fixes.

The implementation plan must answer these with source evidence, concrete tests and explicit limits. Material changes to the approved behaviour return to the user for discussion.
