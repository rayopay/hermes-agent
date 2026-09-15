# Rayopay downstream maintenance

This is a maintained fork of [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent), not a replacement platform. Preserve upstream attribution, licensing, configuration semantics, and native lifecycle safeguards while carrying the smallest necessary set of reviewed fixes.

**Current scope:** qualify and roll out RP-HERMES-001: remove the blanket PR-comment cooldown and hold only ambiguous interrupted implementation recovery for owner reconciliation. Publication, merge and local deployment are authorized, subject to the remaining technical gates; the change is not yet accepted or deployed. CI-policy discovery and triage changes remain queued separately.

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
   - Status: **In review — focused tests pass; full-suite/release qualification outstanding**.
   - Implementation PR: [rayopay/hermes-agent#3](https://github.com/rayopay/hermes-agent/pull/3).
   - Implementation checkpoint: `7d6d752af0e00633178d187711846bceed57b215`, based on `d77d61287012a53fe915c11e950bbcc72a0a7630`; subsequent documentation-only commits do not change this executable checkpoint.
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

Remove the blanket PR-comment cooldown so eligible work can continue on the same
card and PR. Retain a durable native hold only when an implementation run is
interrupted before handoff and newly recorded evidence makes publication uncertain;
the owner reconciles existing work before resuming it. Existing claim, review,
authentication, dependency and retry protections remain in place.

**Design and decisions:** [RP-HERMES-001.md](RP-HERMES-001.md) records the rationale,
chosen behaviour, rejected alternatives, acceptance criteria, implementation,
verification checkpoints, migration/recovery impact and non-goals.

The combined candidate's focused tests pass. Broader qualification and release
gates remain open; neither merge nor deployment has occurred. Documentation-only
reorganisation does not itself require or trigger additional tests.

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
