# RP-HERMES-001 — PR-linked continuation and interrupted-run recovery

Specific design and decision record for [the Rayopay Hermes fork](DOWNSTREAM.md).
Implementation PR: [rayopay/hermes-agent#3](https://github.com/rayopay/hermes-agent/pull/3).

**State:** implemented; qualification of the combined upstream/downstream release is
in progress. Not merged or deployed. Human authorization to finish publication,
merge and local rollout is separate from passing the technical acceptance gates.

## Decisions at a glance

- Remove the blanket 24-hour delay caused by a PR URL in a comment.
- Preserve normal same-card/same-PR continuation and review-return behaviour.
- Hold only ambiguous interrupted implementation runs with newly recorded evidence
  of possible publication; the owner reconciles that work before resuming it.
- Use existing native lifecycle transactions and unblock behaviour, not a new
  scheduler, permission system, timer or force-spawn command.
- Keep the fix small and preserve newer upstream improvements when preparing the
  deployment candidate. Qualify the combination rather than rolling Hermes back.
- Keep code testing, publication/review, merge, and live deployment as distinct gates.

The operator update workflow is specified separately in `RAYOPAY_UPDATES.md` on
branch `ops/reviewed-fork-updates`; it is not part of this runtime behaviour change.
This document reorganisation changes no runtime behaviour or test requirement and
must not itself trigger another test run.


### Observed behavior and motivation

At the audited baseline, `check_respawn_guard` scans recent task comments for a GitHub PR URL. A matching comment triggers `active_pr` for a fixed 86,400-second window on the Ready dispatch lane. Review dispatch is exempt. The guard does not query whether the PR is open, merged, or closed, and an explicit changes-requested event does not override the PR-comment check. Repeating a full PR URL in another comment starts another matching window.

This protects against duplicate publication, but also prevents legitimate continuation of the same task/PR after review or during unfinished finalization. Moving a card to Ready does not prove that it can dispatch. It is not a universal ban on reading or updating a card.

Baseline source: [`hermes_cli/kanban_db_dispatch.py`](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_db_dispatch.py), `_RESPAWN_GUARD_PR_WINDOW` and `check_respawn_guard`.

### Chosen design

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

### Implementation

- Claims record a task-comment row-ID cursor in their existing event payload. Only a newly recorded possible PR reference makes an interrupted implementation uncertain; a reference is not proof of publication or authorship.
- Successful interrupted-run closure installs a sticky native `blocked` event in the same transaction. Coverage includes clean/nonzero exit, timeout, stale/TTL/manual/orphan reclaim, and claim-time dangling-run recovery. Manual reclaim pins the selected status/run/PID/lock so a stale NULL-lock writer cannot erase another recovery hold.
- The owner inspects the failed run and referenced comment, reconciles existing work, and uses native unblock or the appropriate existing lifecycle disposition. This is not an automatic owner agent launch: notification/wake depends on existing subscriptions and delivery settings. New claims start a new evidence cursor, so an old reference does not retrigger the hold.
- Human notifications, owner wake text and dashboard events reflect the actual held state. They do not promise automatic retry while the recovery hold remains active.
- No new schema table/column, config setting, scheduler, permission system, or GitHub request is introduced. Review-source retries and rate-limit exits retain their existing policy; auth, dependency, recent-success and retry-budget protections remain in place.

## Verification and release state

Evidence belongs to the exact executable version and environment exercised; a
later documentation-only edit does not invalidate unchanged executable tests.

- Original fix head: `45f54ac53fa0842d6791ad79ebecb90eb17eedbd`, with
  executable implementation checkpoint `7d6d752af0e00633178d187711846bceed57b215`
  on base `d77d61287012a53fe915c11e950bbcc72a0a7630`.
- Combined candidate: `e8dfb21e4c492acf94038d1dc4896718b9cec12f`, preserving
  installed upstream `2179a279ae04bfadf8efbc49a01ca0abfb738000`.
- The original focused run completed 94 files: **660 passed, 0 failed, 2 skipped**.
  The subsequent old-candidate full run completed with **48,995 passed, 124 failed,
  438 skipped**, plus incomplete/collection/timeout files. It was not green.
  Earlier partial attempts, dependency drift and failure comparisons remain
  preserved in private operational evidence, not erased by newer runs.
- The old candidate's isolated real-worker/GitHub lifecycle proof was completed
  across an initial partial attempt and an authorized continuation. That proves
  the old candidate only; it does not certify the combined candidate.
- The combined candidate's focused run completed 94 files: **662 passed, 0 failed,
  2 skipped**. Its pinned upstream comparison completed 90 files: **608 passed,
  0 failed, 2 skipped**. Both exited successfully with exact source/dependency
  manifests preserved and no surviving scoped test consumers.
- Independent bounded static review of the combined candidate found no actionable
  P1/P2 combination regression. This is not a full runtime-acceptance verdict.
- The combined full canonical run completed 4,182 files: **49,313 passed,
  135 failed, 482 skipped**, exit 1. Replaying its 42 failed/incomplete files on
  pinned upstream yielded **1,452 passed, 133 failed, 22 skipped**, exit 1.
  The two initially candidate-only failed assertions also reproduced on unchanged
  upstream in a bounded two-file diagnostic. Equal/advanced timestamp controls
  reproduced the cache/poll behaviour on both sides; the relevant source files
  are identical. No candidate-unique failed test remains in these comparisons.
- This is **not a green full suite**: shared upstream/environment failures remain,
  with two browser-file timeouts and a missing-`which` collection error on both
  sides. Source/dependency manifests remained unchanged and all scoped processes
  settled. Documentation-only changes did not trigger the tests; the diagnostic
  addressed failures from the already-authorized qualification run.

Remaining release gates: classify full-run failures/incomplete files, complete
needed combined-candidate real-worker lifecycle proof, verify exact published-head
review/checks, and rehearse the pinned native update and recovery before controlled
local deployment. Merge and deployment remain unperformed at this checkpoint.
Unrecorded external publication cannot be discovered from comments alone; this is
not an exactly-once publication guarantee. Pre-existing process-tree,
missing/corrupt-run and postcommit bookkeeping limitations are not certified merely
by passing the focused tests.

## Migration and recovery

**Migration and rollback:** new claim-event metadata is additive. Existing in-flight runs without a cursor use an inclusive start-time fallback, which can conservatively hold a same-second pre-existing reference. Old history is never deleted or bulk rewritten. A prior runtime ignores the new cursor and understands the existing blocked/unblocked events, but restores the old PR-comment timer. Before any rollout or rollback, drain workers, preserve exact code/dependencies and board backups, rehearse on isolated board copies, and verify held-card state; no deployment or rollback drill has occurred in this change.

## Non-goals

Changing triage recurrence or CI acceptance policy; weakening completion checks;
creating replacement cards or sibling PRs to evade an unfinished original scope;
automatically redispatching unresolved holds; or treating merge authorization as
permission to skip safe-drain, backup, verification or recovery gates.
