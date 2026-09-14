# Rayopay downstream maintenance

This is a maintained fork of [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent), not a replacement platform. Preserve upstream attribution, licensing, configuration semantics, and native lifecycle safeguards while carrying the smallest necessary set of reviewed fixes.

**Current scope:** RP-HERMES-001 implementation and isolated validation: remove the blanket PR-comment cooldown and hold only ambiguous interrupted implementation recovery for owner reconciliation. This behavior is authorized for development, not yet accepted or deployed. CI-policy discovery and triage changes remain queued separately.

**Tracking PR:** [rayopay/hermes-agent#1](https://github.com/rayopay/hermes-agent/pull/1) — documentation groundwork; implementation and deployment are tracked separately below.

## Baseline and status conventions

- Audited baseline: Hermes v0.21.2, upstream commit [`6bc0e9e6df1be528dca42b4ae720026192896662`](https://github.com/NousResearch/hermes-agent/commit/6bc0e9e6df1be528dca42b4ae720026192896662).
- The documentation branch starts from that exact revision. The fork's default branch can contain newer upstream commits; that is not evidence that they have been validated or deployed.
- Baseline verification consisted of source inspection, isolated routing/guard checks, and read-only GitHub API checks. It was not a full upstream test-suite run or an end-to-end lifecycle acceptance test. Private operational evidence is not published here.
- Before implementing each change, recheck current upstream code and existing upstream PRs to avoid carrying a fix that already exists.
- Each implementation PR must update its entry with the exact base/head, chosen behavior, tests, migration impact, and rollback requirements. Record merge and deployment evidence separately.

Status meanings:

- **Proposed:** a direction and acceptance criteria, not a finalized API or working feature.
- **Authorized development:** the behavior is approved and local implementation is active; no implementation PR is open yet.
- **In implementation:** an implementation PR exists and work is actually active.
- **In review:** code and verification evidence are submitted, with outstanding review identified.
- **Merged:** accepted into the fork; not necessarily deployed.
- **Deployed:** an approved release is installed and the running fleet has been verified.
- **Upstreamed / retired:** an equivalent upstream change has been verified and the redundant downstream patch removed.

## Change register

1. **RP-HERMES-001 — PR-linked continuation without a blanket 24-hour delay**
   - Priority: first implementation.
   - Status: **Authorized development — conservative interrupted-handoff recovery selected**.
   - Implementation PR: not opened.
   - Merge / deployment: neither performed.
2. **RP-HERMES-003 — exact-head CI acceptance without mandatory paid policy discovery**
   - Priority: queued after RP-HERMES-001.
   - Status: **Proposed — implementation not started**.
   - Implementation PR: not opened.
   - Merge / deployment: neither performed.
3. **RP-HERMES-002 — meaningful block recurrence and explicit triage recovery**
   - Priority: queued after RP-HERMES-003.
   - Status: **Proposed — implementation not started**.
   - Implementation PR: not opened.
   - Merge / deployment: neither performed.

## RP-HERMES-001: PR-linked continuation

### Observed behavior and motivation

At the audited baseline, `check_respawn_guard` scans recent task comments for a GitHub PR URL. A matching comment triggers `active_pr` for a fixed 86,400-second window on the Ready dispatch lane. Review dispatch is exempt. The guard does not query whether the PR is open, merged, or closed, and an explicit changes-requested event does not override the PR-comment check. Repeating a full PR URL in another comment starts another matching window.

This protects against duplicate publication, but also prevents legitimate continuation of the same task/PR after review or during unfinished finalization. Moving a card to Ready does not prove that it can dispatch. It is not a universal ban on reading or updating a card.

Baseline source: [`hermes_cli/kanban_db_dispatch.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_db_dispatch.py), `_RESPAWN_GUARD_PR_WINDOW` and `check_respawn_guard`.

### Proposed fix

Remove the blanket PR-comment timer rather than add a special continuation override. A link in a comment is context, not proof that publication succeeded, that assigned scope is complete, or that eligible Ready work should wait. Preserve comments, the existing card and PR, and native review/claim/retry protections. Do not replace the timer with a shorter delay or an unrestricted force-spawn path.

First exercise the existing lifecycle protections against competing claims, review-requested changes, deliberate reopening, publication followed by worker exit before handoff, and repeated failures. If removing the timer exposes a real recovery or handoff gap, address that specific gap through existing lifecycle machinery. Do not add a new permission system, scheduler, or manual recovery requirement without demonstrating that it is necessary. No new configuration key, command, or database field is assumed.

Explain the reason for each code change in comments, including which invariant owns the remaining protection. A claim lock prevents competing current claimants; it does not by itself establish exactly-once external publication. Do not describe the removal as publication-safe until the interrupted-handoff scenario is verified.

### Acceptance criteria

- Eligible Ready and Review work can proceed despite a fresh or repeated PR URL comment, without a PR-link-based wait or a special override.
- Changes-requested continuation and deliberate reopening preserve the same card/PR and historical evidence; neither a URL nor worker exit implies completion or reviewer readiness.
- Competing dispatch attempts cannot create multiple current DB claimants or duplicate live workers in the exercised native path. Preserve existing worker, claim and dependency exclusions; identify any pre-existing process-tree/runtime limitations separately.
- Publication followed by worker exit before native handoff has an explicit, tested recovery behavior. Show what prevents sequential duplicate publication, or clearly report that safety criterion as unmet rather than presenting retry budgets or advisory context as idempotency.
- Review-lane behavior, authentication/rate-limit guards, unrelated recent-success safeguards, and bounded failure accounting remain intact.
- Older cards retain historical evidence; any migration, fallback, or additional recovery requirement is explicit.
- Real native-path regression tests cover competing claims, requested changes, reopening, clean/nonzero interrupted handoff, and repeated failure. Distinguish injected spawn/provider boundaries from actual worker execution and GitHub lifecycle proof. User-facing diagnostics distinguish eligibility, a hold, and a verified running worker.

### Pre-implementation safety experiment

Fork base: `d77d61287012a53fe915c11e950bbcc72a0a7630`. The preserved pre-implementation characterization experiment exercised real disposable SQLite state and native reclaim-through-dispatch, replacing only the `active_pr` guard result in its experimental arm. Spawn and publication were inert test boundaries, not real worker/GitHub execution. Its source SHA-256 is `b79ba8a87567393b859801b77317c3b4d62d1d2211ae4f865909951e81efd43a`; these historical results must not be presented as candidate-fix test results.

Two identical-source runs each passed all 14 cases. Both clean and nonzero worker exits after synthetic PR-comment evidence allowed a second committed run in the same dispatch tick when the timer was removed. The timer-intact arms deferred those tasks. Normal no-publication retries, same-card review and changes-requested handoff, competing direct-claim refusal, stale-run handoff refusal, and exhausted retry budgets behaved as asserted. These are characterization results, not proof that duplicate publication is prevented; real concurrent worker/process-tree behavior and GitHub publication were not exercised.

**Approved recovery behavior:** remove the normal PR-link delay. When an implementation run is interrupted before handoff and there is evidence of possible publication, use a durable native hold so the owning agent can reconcile the existing work before resuming. This is an uncertainty check, not proof that a PR was published. Ordinary continuation, requested changes and deliberate reopening do not acquire a new wait merely because a PR link exists. Reuse existing lifecycle resolution rather than introduce a new scheduler or permission system. A hold can remain unresolved until the owner acts; there is no automatic 24-hour release.

### Implemented candidate and verification

- Claims record a task-comment row-ID cursor in their existing event payload. Only a newly recorded possible PR reference makes an interrupted implementation uncertain; a reference is not proof of publication or authorship.
- Successful interrupted-run closure installs a sticky native `blocked` event in the same transaction. Coverage includes clean/nonzero exit, timeout, stale/TTL/manual/orphan reclaim, and claim-time dangling-run recovery. Manual reclaim pins the selected status/run/PID/lock so a stale NULL-lock writer cannot erase another recovery hold.
- The owner inspects the failed run and referenced comment, reconciles existing work, and uses native unblock or the appropriate existing lifecycle disposition. This is not an automatic owner agent launch: notification/wake depends on existing subscriptions and delivery settings. New claims start a new evidence cursor, so an old reference does not retrigger the hold.
- Human notifications, owner wake text and dashboard events reflect the actual held state. They do not promise automatic retry while the recovery hold remains active.
- No new schema table/column, config setting, scheduler, permission system, or GitHub request is introduced. Review-source retries and rate-limit exits retain their existing policy; auth, dependency, recent-success and retry-budget protections remain in place.

**Focused verification:** canonical `scripts/run_tests.sh`, credential-free scratch HOME, core/dev/messaging dependencies from unchanged `uv.lock`: all 94 Kanban-named test files completed with **660 passed, 0 failed, 2 skipped**. Tests use real native SQLite, claim/reclaim/lifecycle transactions and notification formatting; worker spawn, provider publication and signals are inert boundaries. Independent review found two recovery bypasses and contradictory diagnostics; these were fixed with causal red/green regressions, and the subsequent scoped review found no remaining change-caused P1/P2 blocker. New Python files pass Ruff F checks; `git diff --check` passes.

**Full-suite limitation:** a separate full-repository attempt was stopped at its 1,800-second bound (exit 124), with source hashes unchanged and no final full-suite verdict. It had recorded 27 test failures plus collection errors. Replaying all 29 failing/error files on the unchanged base in the same dependency environment reproduced 26 identical failing test IDs and the 11 collection-error files. Missing optional ACP/Anthropic dependencies explain several failures; do not classify every failure as dependency-only. The remaining gateway MCP test passed on both baseline and candidate reruns; its original intermittent failure is retained. This is not a full-suite pass or release qualification.

**Migration and rollback:** new claim-event metadata is additive. Existing in-flight runs without a cursor use an inclusive start-time fallback, which can conservatively hold a same-second pre-existing reference. Old history is never deleted or bulk rewritten. A prior runtime ignores the new cursor and understands the existing blocked/unblocked events, but restores the old PR-comment timer. Before any rollout or rollback, drain workers, preserve exact code/dependencies and board backups, rehearse on isolated board copies, and verify held-card state; no deployment or rollback drill has occurred in this change.

**Outstanding acceptance:** CodeRabbit/PR review, full-suite qualification, actual isolated worker/GitHub lifecycle proof and approved rollout/rollback remain outstanding. Unrecorded external publication cannot be discovered from comments alone; this is not an exactly-once publication guarantee. Pre-existing process-tree, missing/corrupt-run and postcommit bookkeeping limitations are not certified by these tests. Live boards, CI acceptance and triage policies remain outside the change, and deployment requires separate approval.

**Non-goals:** changing triage recurrence, weakening completion/CI checks, creating replacement cards or sibling PRs, or deploying a runtime change during this validation phase.

## RP-HERMES-003: CI acceptance and policy discovery

### Observed behavior and motivation

For an explicit PR completion contract, the native acceptance collector reads classic branch protection and the active-rules endpoint to discover required checks, then evaluates exact-head check runs and legacy statuses. Unreadable policy rejects completion.

GitHub can allow check-run reads while returning a plan-related HTTP 403 for rules on private repositories. This is a **required-policy discovery** dependency, not a general requirement to buy a plan merely to read CI. An individual account upgrade is not interchangeable with the applicable organization plan. Local-only contracts are a distinct existing mode, not a workaround for downgrading an already declared PR contract.

Baseline source: [`hermes_cli/kanban_pr_acceptance.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_pr_acceptance.py), `collect_acceptance`, and [`hermes_cli/kanban_pr_acceptance_store.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_pr_acceptance_store.py).

### Proposed fix

Add an explicitly selected, operator-controlled required-check policy source alongside native GitHub policy discovery. It must declare a nonempty check set scoped to the exact repository and target branch, include check-provider identity where required, and record its provenance/version in acceptance receipts. Keep the implementation at the shared completion boundary so tools, CLI, review approval, and dashboard cannot disagree.

This operator policy is an explicit alternative authority, **not** a claim that unreadable GitHub rules are absent. Never switch to it automatically on a 403 or another API error. Its exact configuration interface and change-control semantics require design review.

### Acceptance criteria

- Explicitly selected operator policy permits verification without requiring the paid rules endpoint; the original GitHub-discovery mode remains available.
- Required checks are nonempty, repository/base-scoped, and unavailable for workers to weaken through comments or completion metadata.
- Missing, pending, failed, cancelled, stale, skipped, or neutral required evidence cannot satisfy completion. Optional check failures do not automatically veto valid required evidence.
- Exact-head matching, check-App identity, pagination completeness, PR head/base revalidation, and transactional run ownership remain enforced.
- Policy changes during collection invalidate the receipt or require a retry; policy identity and evidence remain auditable.
- Unreadable policy, malformed configuration, and API failures reject completion with actionable diagnostics. Existing PR contracts are preserved, not changed to local-only.

**Non-goals:** changing GitHub billing or visibility, treating empty policy as acceptance, or disabling required CI checks.

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
