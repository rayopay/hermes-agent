# RP-HERMES-003: Reported CI acceptance without policy discovery

- **Decision:** accepted; implementation submitted for review, not merged or deployed.
- **Implementation PR:** [rayopay/hermes-agent#5](https://github.com/rayopay/hermes-agent/pull/5).
- **Branch:** `fix/ci-status-without-policy-discovery`.
- **Implementation base:** `d77d61287012a53fe915c11e950bbcc72a0a7630`.
- **Collector implementation checkpoint:** `0b3bd403443b7264ba63d274be414a8a85034ff5`.
- **Register:** [DOWNSTREAM.md](../../DOWNSTREAM.md#rp-hermes-003-ci-acceptance-and-policy-discovery).
- **User-facing contract:** [Kanban PR completion contracts](../../website/docs/user-guide/features/kanban.md#pr-completion-contracts).

This record preserves the agreed design and rejected alternatives. PR reviews and exact-head verification evidence track delivery separately; an accepted design is not merge or deployment approval.

## Problem and historical context

The upstream collector discovers required checks through branch-protection GraphQL and active-rules APIs before evaluating a PR's CI. On private repositories, policy discovery can fail with a plan-related HTTP 403 even when check runs and commit statuses remain readable. The requirement is to remove that policy-discovery dependency, not to bypass failed CI or turn API errors into success. Repository read access and suitable authentication are still necessary.

The audited implementation is [the upstream collector at the audited baseline](https://github.com/NousResearch/hermes-agent/blob/6bc0e9e6df1be528dca42b4ae720026192896662/hermes_cli/kanban_pr_acceptance.py). Historical inspection identified [commit ac07da267458becb6ec9d7b85c86866a666be025](https://github.com/NousResearch/hermes-agent/commit/ac07da267458becb6ec9d7b85c86866a666be025), `fix(kanban): enforce declared PR acceptance at completion boundary`, as introducing native PR acceptance and policy discovery together. In its [parent revision](https://github.com/NousResearch/hermes-agent/blob/768f42f72ff09ff37cb857141d94405529ad9bdc/hermes_cli/kanban_db.py), completion checked task eligibility, dependencies and run ownership but did not natively query GitHub CI. There is therefore no earlier native basic-CI gate to restore by reverting only to the preceding implementation.

## Decision

Keep native PR-linked acceptance, but evaluate **reported CI for the exact current PR head**, without discovering repository policy and without introducing replacement configuration.

### 1. Remove policy discovery, not the completion contract

No branch-protection or active-rules request is made. No operator-maintained required-check list, task-level override, feature flag or 403 fallback is added. Existing local-only contracts remain local-only; this change does not downgrade a declared PR contract.

Contract validation, permanent PR binding, persisted acceptance receipts and the shared native completion boundary remain in place. The database/store implementation and run-ownership safeguards are unchanged.

### 2. Select current evidence without masking unrelated checks

1. Resolve and validate the PR against its persisted completion contract.
2. Capture head SHA, base branch, state and merged flag.
3. Paginate check runs and legacy commit statuses scoped to that SHA.
4. Select the latest check-run ID per name/App/check-suite identity and the latest legacy status ID per context. Keep check runs and legacy statuses separate even when names match.
5. Reject malformed identities, wrong-head evidence, duplicate IDs and incomplete/inconsistent pagination rather than choosing a favorable row. Numeric identifiers must be positive integers, not booleans.
6. Re-read PR identity after all pages; reject a changed head, base or state. Valid PR state combinations are open/unmerged or closed/merged; closed/unmerged is not accepted.

A newer result supersedes older history within its identity; a same-named result from another App, suite or evidence type does not erase a failure.

### 3. Accept only successful, resolved reported evidence

- Require **at least one successful** check run or legacy status.
- Completed **skipped** or **neutral** check runs may coexist with success. Conditional jobs should not block solely because they did not execute, but skipped/neutral results alone do not establish success.
- Any selected failure blocks, including a check that GitHub policy might call optional. There is deliberately no required-versus-optional distinction.
- Pending/incomplete, cancelled, timed-out, action-required, unknown, stale or malformed results do not permit completion.
- No reported evidence, unreadable APIs or invalid responses are not green. Do not suppress failed API calls or accept partial evidence.

### 4. Keep ownership and evidence guarantees

Network collection remains outside SQLite write transactions. The lifecycle owner revalidates the captured task/run/status/contract under its native lock before persisting a receipt or completing a task; an ownership race must not complete a successor run.

Receipts identify the `reported-ci` basis, exact head and selected evidence. Failure diagnostics identify the collection phase without persisting raw `gh` stderr or exceptions that might contain secrets. Only read-only GitHub APIs are used.

## Alternatives considered and rejected

### Operator-managed required-check policy

The initial proposal retained policy discovery by default and offered an explicitly configured operator policy where GitHub rules were unavailable. This was rejected: it would replace the subscription dependency with configuration, ownership and drift-management complexity that was not requested. There is no replacement policy file or hidden fallback.

### Revert the entire introducing change

A complete revert would also remove native CI enforcement, declared PR contracts/binding and acceptance receipts. CI responsibility would move back to agent instructions and human review. This was discussed and rejected in favor of the narrow change, which preserves native guarantees unrelated to policy discovery.

### Require every check to conclude success

Rejected because legitimate conditional jobs can finish skipped or neutral. Those outcomes are permitted only alongside an actual success; accepting an all-skipped/all-neutral set was also rejected.

### Ignore optional failures or treat absent evidence as success

Rejected. Without policy discovery, Hermes cannot reliably classify required versus optional checks or prove that an expected but unreported job ran. The selected rule evaluates everything it can actually observe and fails closed when evidence is unavailable.

## Consequences and accepted limitations

- CI reads no longer depend on paid/private-repository rules discovery. This does not claim every account/token can read every repository or establish private-plan compatibility through a billing-plan test.
- An optional reported failure blocks completion. Operators must fix/re-run or otherwise resolve it; this patch supplies no override.
- An expected check that never appears is undetectable. A successful bot status with a skipped check can satisfy this rule even if no build/test workflow ran. **Reported-CI acceptance is not certification of test coverage.**
- Acceptance is a completion-time snapshot, not a mergeability decision, continuous monitoring or a distributed transaction with GitHub. Final identity revalidation prevents transferring evidence across an observed PR change; it cannot prevent future CI/PR changes.
- Review approval, merge authorization and deployment qualification remain separate obligations.

## Verification record

### Original implementation qualification

Canonical `scripts/run_tests.sh`, under credential-free scratch HOME with isolated per-file processes, completed all 90 Kanban-named test files: **701 passed, 0 failed, 2 skipped**. The acceptance file contained 97 passing cases covering native SQLite completion, local HTTP pagination, tool dispatch, CLI completion, review ownership and the dashboard HTTP endpoint. The standalone lifecycle probe, Ruff F checks and worktree-local environment `pip check` passed. Tracked-source hashes and the environment's non-bytecode manifest remained unchanged through those runs.

The unchanged-base reported-CI regression run recorded 63 failures and 2 passes; some failures concerned newly introduced receipt fields, not acceptance outcomes. Independent review subsequently identified ambiguous duplicate legacy-status IDs and invalid identifier/PR-state acceptance. Native regressions reproduced 13 unsafe acceptances before correction. Independent re-review found those issues addressed, with 31/31 bounded probes passing and no remaining actionable blocker within that review's scope.

A separate read-only canary exercised actual public-fork GitHub/`gh` responses without transitioning a live card. A successful bot status plus a skipped check satisfied the selected rule. That was response-shape evidence, not proof of a build/test workflow or private-repository billing-plan coverage.

These are scoped Linux results, not the full repository suite, cross-platform certification, complete dependency-lock qualification, release qualification or a rollback drill.

### PR review: standalone probe execution authority

[Review comment 4020766868](https://github.com/rayopay/hermes-agent/pull/5#discussion_r4020766868) correctly observed that a delegated child cannot perform the probe's native board mutations. Its proposed removal of `HERMES_DELEGATED_CHILD_CONTEXT` is not adopted: disposable state does not grant permission to remove an inherited write fence.

The probe now checks the native delegated-process predicate before importing the fixture or opening a database, and exits with an actionable instruction to ask an authorized top-level test process to run it. The regression executes the real script with the marker present and an unavailable fixture directory, requiring a clear refusal before fixture loading and no HOME creation. The parent-run lifecycle probe remains the positive execution path. The native guard itself is unchanged.

The new refusal regression failed before the preflight change and passed afterward. Post-review canonical verification ran the original 90-file Kanban inventory plus the new probe regression: **702 passed, 0 failed, 2 skipped across 91 files**. The authorized top-level lifecycle probe and Ruff F checks passed again; source hashes were unchanged during verification. The collector, store and native guard were not modified by this review follow-up. The original qualification limits still apply.

### Fork-safe CI qualification

[Rayopay Kanban CI](../../.github/workflows/rayopay-kanban-ci.yml) is a bounded Linux qualification lane for this branch, not a replacement for the full upstream CI or release pipeline. It uses a standard GitHub-hosted Ubuntu runner, Python 3.11.15 supplied by a SHA-pinned `setup-python` action, and upstream's pinned uv 0.9.28. Dependencies are installed with `--locked`, the existing `dev` and `messaging` extras, and no project installation; dependency manifests must remain unchanged. Messaging dependencies are necessary for the included gateway wake tests.

The workflow discovers Kanban-named test files (including the standalone-probe regression), rejects an empty selection, runs the canonical per-file runner with two workers and no retry, and runs the lifecycle probe and affected-file Ruff F checks. Logs are retained as per-head artifacts. It has only `contents: read`, does not persist checkout credentials, and uses no repository secrets, publication steps or deployment environments.

The bootstrap push trigger is restricted to this implementation branch. Manual `workflow_dispatch` supports deliberate exact-head qualification. A correction may use GitHub's `[skip ci]` push directive followed by an explicit dispatch of this workflow to avoid starting the inherited broad/specialized-runner fan-out; that is a declared scoped test run, not a claim that skipped or cancelled upstream lanes passed. No repository-wide workflow settings or merge requirements are changed. Expanding this lane to other branches or replacing inherited CI is a separate maintenance decision.

The first remote attempt failed before tests because uv's pinned download catalog lacked Python 3.11.15; supplying Python separately corrected that setup issue. The next attempt exposed missing messaging dependencies (692 passes, one failure and one collection error). Those failed attempts remain evidence; subsequent run URLs, exact heads, actual counts and final review coverage are recorded in the PR rather than assumed from workflow configuration. Remote CI results do not establish full-suite or deployment qualification.

## Migration, rollback and delivery boundaries

No schema/configuration migration or automatic card recovery is introduced. Existing bindings and historical receipts remain intact. Previously held cards require a separately authorized native disposition; this PR does not edit live boards.

Rollback restores the prior collector and corresponding documentation, reinstating policy discovery for future attempts. It does not undo completed tasks or rewrite historical `reported-ci` receipts. A deployment rollback must account for the actual release/dependency/state snapshot, not merely switch source revisions.

Before merge, obtain final-head review and explicit test/release evidence appropriate to the fork; bot status is not test qualification. Merge requires separate authorization. Deployment then requires a pinned candidate, release-route/dependency qualification, backups, worker drain, rollback preparation and verified fleet activation under the maintenance procedure in `DOWNSTREAM.md`.

**Non-goals:** changing billing or repository visibility, adding operator policy, weakening delegated/run-ownership guards, changing RP-HERMES-001/RP-HERMES-002, merging, deploying, or recovering live cards as part of this PR.
