# RP-HERMES-002: Meaningful block recurrence and orchestrator recovery

## Status and scope

Tracking PR: [rayopay/hermes-agent#6](https://github.com/rayopay/hermes-agent/pull/6) (ready for review; implementation and verification in progress, not merge/deployment ready).

**Behaviour approved; incremental implementation in progress.** This document formalises the agreed RP-HERMES-002 behaviour. Counter protection and native identity/classification increments have test and independent review evidence below; the complete orchestrator recovery feature, supported recovery surfaces, integration, merge, deployment and live-card recovery remain outstanding.

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

## Native identity/classification increment: verified evidence

Base of this increment: `fad0038417e4bb65d95467b1010dccdb2ee3a359`. This is a partial native implementation, not an operational triage-release feature.

- An additive `task_recovery` projection and immutable native events retain server-generated blocker identities, original/effective report attribution, per-identity recurrence, conservative historical lower bounds and revision-bound classification. Category/prose changes do not mint cheaper identities.
- `get_recovery_state` is read-only. `classify_blocker` requires a top-level native actor, an exact current observation and an unclaimed held card. Classification changes attribution, not status or dispatcher failure budget. Worker/delegated contexts are refused; existing cooperative runtime fences are not a claim of project-level RBAC or same-UID confinement.
- Both direct classification of an already-held legacy card and first native reporting from a legacy card retain historical protection. The first tracked report immutably records its inherited budget, including dependency-first reports. Dependencies remain uncounted. Audit replay rejects projection-only identity, attribution or lower-bound corruption; legitimate zero-count historical identities remain valid.
- Successful existing native completion closes the accounting epoch in its own transaction, bound to the actual completion event and revision. All identities, reports and corrections remain inspectable. Deliberate reopening starts fresh accounting consistent with baseline. Ordinary unblock, status changes and classification are not resolution events. Existing completion eligibility is unchanged: this does not enable triage finalization.
- Additive native initialization and repeated initialization, actual pre-feature SQLite fixtures, delegated read-only behavior, transaction rollback, stale-observation interception using two native connections, and history-preserving GC are covered. Whole-task deletion remains an explicit history boundary.

Canonical `scripts/run_tests.sh -j2`, retries disabled, admitted worktree-local environment and credential-free disposable HOME: **133 passed, zero failed across 13 files**, including prior increments. Independent specification and quality reviews accepted this bounded checkpoint after corrections.

Causal failures retained rather than waived:

1. Completion followed by deliberate reopening originally yielded candidate `triage/count 2` versus baseline `blocked/count 1`. The unchanged native probe now returns **blocked/count 1 for both**, with separate database dumps.
2. Projection-only invented blocker origins produced ten failing assertions before correction. Thirteen focused cases passed afterward, preserving valid zero-count history.
3. Dependency-first legacy conversion accepted six downward/upward projected-budget corruptions before correction. The final ten added cases pass, including valid legacy/fresh controls and exact no-mutation refusals. Initial ineligible-triage control-fixture failures are retained separately; successful qualification uses eligible native paths.

Final production SHA-256 values:

- `hermes_cli/kanban_db.py`: `864e85ea19691542bde89f852325f282520902f3444c8e64ebb56aa47411794a`.
- `hermes_cli/kanban_db_recovery.py`: `6287a1899a73ff320e8ec2b12a6bb38acf9edbc86e8be0d8b0038a26afaa3b8c`.

All nine checkpoint source/test/fixture hashes were stable during final verification. The 85 installed distribution names/versions remained unchanged; no additional installs or lock changes were made. The earlier environment-preparation limits still apply. Native SQLite selected its existing DELETE-journal safety fallback; this is not WAL, multi-OS or crash/power-loss qualification.

No full-suite, actual tool/CLI recovery, HTTP dashboard recovery, live worker/process-tree settlement, external runtime lease, no-Ready triage finalization, combined-downstream integration, merge, deployment or live-card recovery proof is claimed. These are remaining work, not waived acceptance criteria. Mixed old/new writers remain unsupported; eventual deployment/rollback must drain writers and pair compatible state/code backups. This increment must not be deployed as the complete solution.

## Classification tool/CLI increment: verified evidence

Base: `417a16682d965f67e85595a95848c376376edd46`. Existing `kanban_show` and CLI show expose native recovery state and the observation token. Existing `kanban_unblock` accepts an optional `recovery` object with `action: classify`; CLI `unblock --recovery-json` accepts the same payload for exactly one task, without a pre-operation comment. The shared adapter accepts only native classification fields, not caller-supplied authority. Unsupported actions, explicit null, malformed data, stale observations and worker/delegated mutation attempts fail closed without falling through to ordinary unblock.

Classification responses explicitly say `classified` and `released: false`, with observed native task status. Ordinary unblock with the recovery argument omitted remains compatible. A classification can correct recurrence attribution, but does not resume the card, replenish a resolution cycle or start a worker. Read-only inspection remains available to worker/delegated contexts. Observation fields are not a cross-field concurrent snapshot or a durable promise that status cannot change afterward.

Real registered tool handlers (including framework kwargs), actual CLI parser/command and disposable native SQLite boards were exercised. Final canonical results: **139 passes across 14 native/surface files** and **51 passes across five affected tool/CLI files**, both exit 0. The surface addition contains six parametrized cases with success/refusal loops; four preimplementation failures were retained, including recovery being ignored and conventional unblock incorrectly occurring before the fix. Refusals compare complete initialized-board dumps. Success controls exercise blocked/triage A-to-B-to-A classification, preserved histories and truthful non-release. Independent specification and quality reviews approved this bounded slice.

No dependencies were installed; non-bytecode package-file manifests remained unchanged. Full model-provider schema normalization, external OS CLI invocation, live-worker concurrency and full repository suite are not claimed. This does not implement retry, resolved-resume, process/runtime settlement or no-Ready finalization. CodeRabbit completed review of the preceding native increment at `417a16682d965f67e85595a95848c376376edd46` with no actionable code findings and a docstring-coverage warning; that is not review coverage of this later surface increment. GitHub reported base-branch conflicts at publication preparation; reconciliation and renewed integration verification remain required before merge.

## Native retry/resolved-resume increment: verified evidence

Base: `0b60c78b2c48e0a549d8a4e9a00cca7f67fe1fb1`. Native `recover_task` now supports exact-observation `retry` and `resolved_resume` for a held task. Both user-directed and autonomous bases are recorded with runtime-derived actor, rationale and evidence. Retry preserves the active blocker's budget; resolution advances only that identity's cycle and records its prior count. Other identities retain their budgets without becoming a task-wide veto. Returning to an exhausted identity reapplies its protection. Consumed resolution references and stale observations cannot replenish another cycle.

Recovery uses the actual connection's database path, strict dispatcher exclusion, its own immediate transaction and a fresh observation check. Current claims, open/owned runs and live or inaccessible historical spawned PIDs refuse. The owner must supply a fresh exact-scope settlement attestation covering local attribution, detached descendants, external runtime and workspace ownership. **This is trusted-owner evidence assessment plus recorded-PID probes, not native machine-wide absence proof or verification of reference contents.** Missing scope or Linux host/boot identity does not authorize release; legacy bare-PID reuse can conservatively hold. Native recovery does not spawn, terminate or complete a task.

The shared admission guard evaluates the audited active blocker before claim cleanup and at native/dashboard status paths. Native dispatcher lock/path failures no longer fall through to unlocked dispatch. This intentionally requires a file-backed identifiable database for dispatch; unnamed/in-memory databases return a locked/skipped result, not successful dispatch.

Target-local landing preserves parent gating and Ready/Review continuation without clearing dispatcher failures or changing other cards. Native review rework is recognized through ordered submission, assignment, Review claim, changes request and implementer history, including omitted reviewers and legitimate reassignment. Lost accepted-work continuation remains held. Native completion defines a fresh execution epoch for deliberate reopening; retained old accepted work does not permanently veto new execution.

Consistent legacy blocked/triage reports can enroll and recover their same virtual identity atomically without inventing a new cause. Enrollment binds the original report/payload hash, conservative inherited count, actor and exact observation. Denials and rollback leave no enrollment residue. Original reports, corrections, resolutions and completed epochs remain auditable and retained through GC.

Final canonical deduplicated union: **25 files, 343 passed, zero failed**, two workers, retries disabled. It includes all previous native/surface and affected tool/CLI files plus dispatch/dashboard regressions. Real disposable-board tests cover successful release followed by claim, per-blocker A/B/A protection, same-cause legacy repair, repeated cycles/GC, authoritative review rework, parent/phase routing, stale observations using a second connection, rollback and refusal while a real owned inert process remains alive. That process is held by a parent-controlled pipe, then exited and reaped before retry. No live agent worker was launched.

Causal failures for fresh-epoch recovery, task-wide veto, same-cause legacy enrollment, review rework, omitted reviewers and explicit reviewer reassignment were retained and corrected before independent specification PASS and quality APPROVED. The qualification remains Linux-only for settlement-dependent behavior: **92 items carry the repository's Linux-only marker; seven host-independent release/guard items remain unmarked**. Marker selection was checked on Linux; collection-only wrapper exit 1 is recorded as selection evidence, not a passing execution or native macOS/Windows run. All eleven candidate file hashes and 85 distribution metadata entries remained stable during final execution; package-byte immutability is not claimed.

This native increment still does not expose release through agent-tool/CLI recovery actions, implement no-Ready triage finalization, demonstrate external runtime settlement, qualify the full repository, resolve base-branch conflicts, or authorize merge/deployment/live-card recovery. Those remain required work. No current fleet state has been changed.

## Agent-tool/CLI release increment: verified evidence

Base: `0b971558618b4257ef03386d158540189f65c842`. Existing `kanban_unblock` and CLI `unblock --recovery-json` now forward validated `retry` and `resolved_resume` requests to the reviewed native operation; classification and omitted-recovery conventional unblock remain compatible. Per-action field whitelists reject malformed/unknown requests without fallthrough. Native actor, settlement, locking, evidence, cycle and phase checks are unchanged. Responses preserve native released/eligible/held/status fields and never imply a worker was launched.

Tool and CLI inspection expose unchanged native accounting plus a `settlement_scope` wrapper. The accounting/token and template share a read snapshot; other displayed task fields remain observations. Templates carry exact binding fields but no settlement assertion or evidence reference. The explicit `owner_assessment_required` guidance distinguishes local/detached/external/workspace obligations from native PID checks. Missing host identity makes the template unavailable without breaking classification or read-only show. A template is neither evidence nor permission.

Final corrected union: **353 passed, zero failed across 26 distinct files**, canonical two-worker/no-retry runner. Ten new surface cases exercise actual registered handlers with framework kwargs and parsed CLI commands against real disposable boards, including both initiation modes, exhausted retry refusal, resolved triage recovery followed by an actual native claim/new cycle, same-identity legacy recovery, authority/stale/scope refusals and parent-held Todo responses. The initial 351-pass/25-file run omitted one inherited file; its receipt is retained, and only the corrected complete union qualifies this checkpoint. All seven candidate hashes and 85 distribution metadata entries were stable; independent specification and quality reviews approved this bounded integration.

Provider-normalized dispatch, an installed OS executable, live agent inference and actual external-runtime settlement are not demonstrated by handler/parser tests. Optional test improvements remain: omit the autonomous optional instruction field instead of null, and isolate directed-instruction refusal with otherwise valid settlement evidence. No-Ready finalization, base reconciliation, combined downstream and final integration qualification remain outstanding. No fleet or live-card state changed.

## Technical questions remaining for recovery integration

These are implementation details, not additional user policy decisions:

- Minimal stable blocker identity and per-cycle storage; native versus orchestrator-classified causes; conservative legacy fallback.
- Exact native recovery API and how existing tool/CLI/dashboard calls share it.
- Trusted actor derivation and existing board-access enforcement at every entry point.
- Freshness and finite autonomous recovery rules that prevent relabelling or recycled evidence from defeating the breaker.
- Phase-aware continuation/finalization without dispatching completed work.
- Transactional stale-event/claim fencing and reliable process/runtime quiescence checks.
- Compatibility with historical triage specification, notifier events, schema upgrades, rollback and the other downstream fixes.

The implementation plan must answer these with source evidence, concrete tests and explicit limits. Material changes to the approved behaviour return to the user for discussion.
