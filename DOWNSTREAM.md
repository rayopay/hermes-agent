# Rayopay downstream maintenance

This is a maintained fork of [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent), not a replacement platform. Preserve upstream attribution, licensing, configuration semantics, and native lifecycle safeguards while carrying the smallest necessary set of reviewed fixes.

**Current scope:** RP-HERMES-002 remains under integration qualification on pinned fork main `36f2e2ec916a97f960bfb356f92eb549d5e58621`. Reviewed increments implement blocker identity/classification, native and agent-tool/CLI retry/resolved-resume and bounded direct-triage finalization, scoped owner-attested settlement, dispatch exclusion and phase/history safeguards. The pinned base includes merged RP-HERMES-003 ([PR #5](https://github.com/rayopay/hermes-agent/pull/5)): reported exact-head CI acceptance without required-check policy discovery. Merge is not deployment evidence. RP-HERMES-001 remains open in [PR #3](https://github.com/rayopay/hermes-agent/pull/3) and is not integrated here. Final integration qualification remains outstanding; this branch is not merge/deployment-ready.

**Tracking PR:** [rayopay/hermes-agent#1](https://github.com/rayopay/hermes-agent/pull/1) — documentation groundwork; implementation and deployment are tracked separately below.

## Baseline and status conventions

- Audited baseline: Hermes v0.21.2, upstream commit [`6bc0e9e6df1be528dca42b4ae720026192896662`](https://github.com/NousResearch/hermes-agent/commit/6bc0e9e6df1be528dca42b4ae720026192896662).
- The documentation branch starts from that exact revision. The fork's default branch can contain newer upstream commits; that is not evidence that they have been validated or deployed.
- Baseline verification consisted of source inspection, isolated routing/guard checks, and read-only GitHub API checks. It was not a full upstream test-suite run or an end-to-end lifecycle acceptance test. Private operational evidence is not published here.
- Before implementing each change, recheck current upstream code and existing upstream PRs to avoid carrying a fix that already exists.
- Each implementation PR must update its entry with the exact base/head, chosen behavior, tests, migration impact, and rollback requirements. Record merge and deployment evidence separately.

Status meanings:

- **Proposed:** a direction and acceptance criteria, not a finalized API or working feature.
- **Locally verified:** implementation and scoped local verification are complete; publication, PR review, release qualification and deployment are separate.
- **In implementation:** an implementation PR exists and work is actually active.
- **In review:** code and verification evidence are submitted, with outstanding review identified.
- **Merged:** accepted into the fork; not necessarily deployed.
- **Deployed:** an approved release is installed and the running fleet has been verified.
- **Upstreamed / retired:** an equivalent upstream change has been verified and the redundant downstream patch removed.

## Change register

1. **RP-HERMES-001 — PR-linked continuation without a blanket 24-hour delay**
   - Priority: first implementation.
   - Status: **In review — open implementation, not integrated in this candidate**.
   - Implementation PR: [rayopay/hermes-agent#3](https://github.com/rayopay/hermes-agent/pull/3).
   - Merge / deployment: neither performed.
2. **RP-HERMES-003 — reported exact-head CI acceptance without policy discovery**
   - Priority: approved as a narrow, independent change.
   - Status: **Merged into fork main — deployment not established**.
   - Implementation PR: [rayopay/hermes-agent#5](https://github.com/rayopay/hermes-agent/pull/5); branch `fix/ci-status-without-policy-discovery`.
   - Base: `d77d61287012a53fe915c11e950bbcc72a0a7630`; implementation checkpoint: `0b3bd403443b7264ba63d274be414a8a85034ff5`.
   - Merge: `74fa18f59f9671012788db5abf52ffa14e78d1a7`; included in pinned main `36f2e2ec916a97f960bfb356f92eb549d5e58621`. Deployment: not established.
3. **RP-HERMES-002 — meaningful block recurrence and explicit triage recovery**
   - Priority: third downstream change; developed independently of RP-HERMES-001/003. RP-003 is now in the pinned base; RP-001 remains open and separate. Combined qualification is required before release.
   - Status: **In implementation — pinned-main reconciliation and reported-CI composition tested and independently reviewed; recovery-specific provider/executable and final qualification outstanding**.
   - Design: [Block recurrence and orchestrator recovery](docs/design/rp-hermes-002-triage-recovery.md).
   - Scope includes user-directed and evidence-backed autonomous orchestrator recovery, with worker restrictions, preserved history and no replay of accepted work.
   - Implementation PR: [rayopay/hermes-agent#6](https://github.com/rayopay/hermes-agent/pull/6), ready for review; not merge/deployment ready. Latest deduplicated bounded qualification: **716 passed across 43 files**, retaining the prior 29-file inventory and adding incoming acceptance/routing/registry/schema coverage. Independent specification and quality reviews approved the pinned-main reconciliation and reported-CI finalization composition. Exact evidence and remaining limits are in the design document; totals are not cumulative feature coverage.
   - Merge / deployment: neither performed. Live-board recovery is outside this development approval.

## RP-HERMES-001: PR-linked continuation

### Observed behavior and motivation

At the audited baseline, `check_respawn_guard` scans recent task comments for a GitHub PR URL. A matching comment triggers `active_pr` for a fixed 86,400-second window on the Ready dispatch lane. Review dispatch is exempt. The guard does not query whether the PR is open, merged, or closed, and an explicit changes-requested event does not override the PR-comment check. Repeating a full PR URL in another comment starts another matching window.

This protects against duplicate publication, but also prevents legitimate continuation of the same task/PR after review or during unfinished finalization. Moving a card to Ready does not prove that it can dispatch. It is not a universal ban on reading or updating a card.

Baseline source: [`hermes_cli/kanban_db_dispatch.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_db_dispatch.py), `_RESPAWN_GUARD_PR_WINDOW` and `check_respawn_guard`.

### Proposed fix

Replace comment age as the sole authority for blocking legitimate continuation with an explicit, audited continuation decision integrated into the native lifecycle. Reuse the existing task and PR identity; distinguish continuation from another attempt to publish the same work. Prefer the smallest correct extension of existing review/dispatch machinery over a parallel scheduler or a general-purpose policy framework.

The implementation design must settle how publication identity and continuation authorization are represented, who may request continuation, how that authorization is consumed or invalidated, and how older cards are treated. No configuration key, command name, or database field is promised by this document.

Do not merely set the timeout to zero, discard PR comments, relabel implementation as review, or add an unrestricted force-spawn path. A shorter configurable delay alone does not resolve the lifecycle mismatch.

### Acceptance criteria

- An authorized changes-requested or unfinished-finalization continuation can become eligible without waiting 24 hours, while reusing its existing task/PR.
- Without valid continuation authorization, duplicate-publication prevention remains effective; adding prose cannot grant authorization or prolong an authorized continuation's delay.
- An existing worker, claim, dependency hold, or runtime/workspace owner cannot be displaced by the continuation path. Concurrent attempts cannot create duplicate workers.
- Authorization is scoped to the task and relevant lifecycle/PR identity, audited, and cannot be replayed after ownership or relevant work identity changes.
- Review-lane behavior, authentication/rate-limit guards, and unrelated recent-success safeguards remain intact.
- Older cards retain their historical evidence and safe behavior; migration and fallback decisions are explicit.
- Real native-path regression tests cover authorized continuation, unauthorized repetition, stale authorization, and competing dispatch attempts. User-facing diagnostics distinguish eligibility, a hold, and a verified running worker.

**Non-goals:** changing triage recurrence, weakening completion/CI checks, creating replacement cards or sibling PRs, or deploying a runtime change in this documentation PR.

## RP-HERMES-003: CI acceptance and policy discovery

Native PR completion previously depended on GitHub branch-protection/rules APIs to discover required checks. On private repositories those reads can require a paid plan even when CI results are readable, preventing completion for reasons unrelated to the reported CI outcome.

The approved narrow change removes policy discovery without replacing it with operator configuration. Hermes still verifies reported checks and commit statuses for the exact current PR head before completing a PR-linked task. It requires at least one success, permits completed skipped/neutral checks alongside success, and rejects blocking or unavailable evidence. PR contracts/binding, receipts and native ownership safeguards remain intact.

The deliberate trade-off is that every reported failure counts, including optional failures, while expected checks that never appear cannot be detected. This is reported-CI acceptance—not proof that a build/test workflow ran, merge approval or deployment qualification. No schema/configuration migration or automatic live-card recovery is introduced.

**Status:** [PR #5](https://github.com/rayopay/hermes-agent/pull/5) is merged into the pinned fork-main base; deployment is not established. Detailed decisions, rejected alternatives, exact acceptance rules, verification scope and rollback considerations live in the [RP-HERMES-003 design decision record](docs/decisions/RP-HERMES-003-reported-ci-acceptance.md).

## RP-HERMES-002: Block recurrence and triage recovery

### Observed behavior and motivation

The baseline `_route_block` increments recurrence when the incoming block **category** equals the previous category. It does not compare the reason or identify the actual unresolved blocker. At `BLOCK_RECURRENCE_LIMIT = 2`, the task enters triage. Unblocking and status-only triage resumption preserve the counter.

Consequently, two different capability problems can be counted as the same loop, and a manually resumed task can immediately return to triage. The real infinite-retry safeguard is valuable; category-level false positives and unclear recovery are the problem.

Baseline source: [`hermes_cli/kanban_db.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_db.py), `_route_block`, `unblock_task`, and `specify_triage_task`.

### Proposed fix

Distinguish the same unresolved blocker from a new one, and provide an explicit, audited recovery operation when a blocker has genuinely been resolved. Define blocker identity and authorization before selecting a schema. Simple free-text equality is insufficient: superficial rewording must not defeat the breaker.

### Acceptance criteria

- Distinct blockers in the same category do not automatically count as the same recurrence.
- Repeated unresolved blockers still trigger bounded escalation, including equivalent or reworded reports.
- Authorized recovery can start a new recovery cycle without deleting previous events, failure evidence, or accepted work.
- Status dragging alone does not silently erase protections or replay completed execution.
- Dependency waiting, dispatcher failure counts, review routing, and active ownership retain their separate meanings.
- Legacy counters and ambiguous blocker identities have an explicit safe migration policy; no blanket board reset is performed.

**Non-goals:** disabling circuit breakers, changing `kanban.failure_limit` as a substitute, or automatically redispatching all triage cards.

## Maintenance and release discipline

1. Prefer official configuration and supported extensions. Carry a core patch only where a real requirement cannot be met safely through those mechanisms.
2. Keep each behavior change in a focused PR with failing-before/passing-after invariant tests. Use upstream's documented test runner, isolated state, and real native call paths; synthetic source-level probes are diagnostic evidence, not release acceptance.
3. Develop and test outside the installed runtime. Keep deployment checkouts clean and pinned to reviewed commits; do not rely on uncommitted edits, stashes, or runtime monkey patches.
4. Integrate upstream changes into a candidate before promotion. Stop on conflicts or failed tests, and revalidate the downstream invariants even when Git merges cleanly. Preserve MIT licensing and contributor attribution.
5. Define and test the release branch/update route before using this fork for deployment. A plain `hermes update` defaults to `main`; `--branch` can select another branch. Fork detection does not automatically reconcile downstream commits with upstream. The README's install commands remain upstream instructions, not a deployment procedure for this fork.
6. Preserve the exact upstream base, downstream head, dependency versions, test results, migration impact, and deployment approval in each release receipt. Review inherited CI/publication workflows before enabling them; this groundwork does not establish a tested release pipeline.
7. Before live activation, back up affected files and databases, drain active workers and runtime owners, validate migrations on isolated copies, and test rollback across code, dependencies, and state. A Git checkout of old code alone is not a database rollback plan.
8. Restart only through approved fleet operations, verify every affected gateway/dashboard revision, and activate the maintenance coordinator last. A merged PR is not proof of deployment.
9. When upstream provides an equivalent fix, prove the same behavior and remove the redundant downstream patch. Report upstream submissions separately; none is created by this groundwork.

## Updating this register

Maintain the stable change IDs. Each implementation PR updates only its relevant entry and links its own evidence; do not reopen or continually rewrite the groundwork PR as a substitute for implementation reviews. Update the deployment field only after actual fleet verification, and preserve superseded decisions in Git history. Future issues follow the same motivation, observed behavior, proposal, acceptance criteria, non-goals, and evidence structure.
