# Rayopay reviewed Hermes updates — small operator workflow

## Scope and authority

The first version scripts **preparation and read-only verification**. Default
handles pause/resume and the maintenance window as explicit observed operations,
not an automatic deployment controller. This is the small approach selected by
Cesar in Discord message `1549516993944617013`. That selection is build/test
approval, not merge, publication, update-source switching or live deployment.

No command in this script runs the updater, touches pause state, restarts a
service, changes installed remotes, pushes, or merges a GitHub PR. Preparation
merges only in a retained maintenance candidate. The official runtime stays
source-clean; Hermes still owns dependency refresh, maintenance and restarts.

## Runnable interface

Use `python3 scripts/rayopay_release.py --help` for command help. All paths must
be absolute, canonical and free of symlink ancestors. Run in a trusted local
maintenance checkout, not an untrusted repository or a shared writer session.

### Prepare

```sh
python3 scripts/rayopay_release.py prepare \
  --repo "$MAINTENANCE_REPO" --installed "$INSTALLED_REPO" \
  --operation "$NEW_OPERATION" --upstream-sha "$UPSTREAM_SHA" \
  --downstream-sha "$DOWNSTREAM_SHA" --upstream-source "$UPSTREAM_SOURCE"
```

Supply exact full lowercase SHAs from independently inspected Git history.
`UPSTREAM_SOURCE` is the canonical official GitHub URL (default), or a clean
local checkout whose HEAD equals `UPSTREAM_SHA`. The maintenance origin must be
`https://github.com/rayopay/hermes-agent.git`. The new operation directory must
have an existing parent outside all supplied checkouts: the maintenance source,
the installed checkout, and any local `UPSTREAM_SOURCE` checkout.

Preparation fetches into the **maintenance repository only**, creates and locks
a detached candidate worktree, and merges the pinned upstream. It preserves both
histories and refuses to omit the installed revision. It preserves conflicts and
returns exit 2 with a blocked receipt; never reset, force or silently drop patches
to turn that into success. `status --operation "$NEW_OPERATION"` reads the receipt.

A successful result is `prepared-not-qualified`. It is not runtime acceptance.
If the installation advances, prepare and qualify the new combination rather
than installing an older passing candidate. Keep earlier proof and conflicts.

### Package

```sh
python3 scripts/rayopay_release.py bundle --operation "$NEW_OPERATION"
```

This writes a create-only prerequisite bundle and a digest manifest. The branch
is `rayopay-release/` followed by the candidate's full SHA. It is not `main`.
Packaging does not run tests, grant approval or publish anything. The operation
is not automatically retried after partial failure: inspect retained metadata
and use a new preparation if needed rather than overwriting artifacts.

### Verify a snapshot

```sh
python3 scripts/rayopay_release.py verify --operation "$NEW_OPERATION" \
  --expected-sha "$REVIEWED_CANDIDATE_SHA" \
  --expected-origin "$EXPECTED_CURRENT_ORIGIN" \
  --expected-branch "$EXPECTED_CURRENT_BRANCH"
```

Obtain these expectations from the separately reviewed operator record, not by
blindly copying whatever the manifest currently says. The origin must be the
canonical official or Rayopay HTTPS Git URL. Verification checks:

- exact clean candidate and installed checkout identities;
- candidate SHA and ancestry against both inputs and installed baseline;
- current branch, one persistent origin URL and standard origin fetch refspec;
- bundle location, digest, exact advertised release ref and prerequisite validity;
- actual Git URL resolution to the bundle without a network fetch.

It reads Git objects/config/refs and artifacts; it does not fetch, update indexes,
repair metadata, approve tests, inspect fleet activity or hold any deployment lock.
Exit 0 reports **`snapshot-verified-not-deployable`** with explicit remaining gates.
Input drift, malformed evidence and inherited Git override variables are refusals.
Receipt JSON is local evidence, not signed authorization. A snapshot can become
stale immediately; repeat checks under maintenance ownership before mutation.

## Exact-version transport and remaining native boundary

`pinned_git_env` uses invocation-scoped `url.BUNDLE_PATH.insteadOf`. A simple
`remote.origin.url` override is unsafe because Git retains multiple URLs and the
persistent first URL may still win. The real fetch test moves the **same release
ref** on the competing remote, then requires the frozen candidate in FETCH_HEAD.
Persistent origin must remain unchanged by the temporary rewrite.

The helper is not a deployment entry point. A future supervised native invocation
must preserve/reject inherited configuration deliberately, require the resolved
URL, hold protected immutable transport throughout the invocation, and verify the
tracking ref as well as FETCH_HEAD. Mode 0400 is not immutable to the file owner;
a hash before/after is detection, not protection against a competing writer.

The installed CLI has no `--sha` gate or general `update --rollback`. Its reviewed
update options include `--branch`, `--switch-branch`, `--yes`, `--keep-stash` and
`--backup`. **Do not run an ordinary unpinned update command as a substitute.**
Non-main release branches avoid fork-main upstream-sync/push behavior. Creating
an absent local branch directly at the candidate may select the already-current
repair path instead of the normal pulled-update/restart path. The exact initial
source/branch transition must be approved and rehearsed before live support.
Native installation-kind admission (including image-managed installations) stays
authoritative. No full native updater or recovery rehearsal is claimed here.

## Explicit maintenance checklist — Default owned

This is the operational sequence, not permission to start it.

1. **Qualify and review.** Select the newer upstream plus downstream fixes; run
   actual regression/lifecycle checks for that exact candidate in isolation.
   Preserve failures and release caveats. Obtain independent review. Old-candidate
   proof and passing script tests do not certify the combined runtime. Publish or
   merge only under the separately authorized repository scope.
2. **Ask for one bounded deployment approval.** Present candidate/tree and bundle
   digest, expected old SHA, exact source/branch transition, affected fleet,
   downtime, backups and recovery limits. Bind the approval to these facts. Drift
   requires reconciliation, not silently broadening the approved version.
3. **Stage supervision before pause.** Recheck native authority without changing
   delegated markers. Use an independent operator process outside every affected
   gateway cgroup. Stage its logs, terminal-result reporting and recovery access;
   a gateway turn cannot supervise its own restart. Acquire one maintenance lock
   and establish cooperative exclusive ownership with other operators. Inventory
   exact services/PIDs, native update plan, boards, workers, descendants, direct
   delegations and runtime leases. Do not log credentials or complete environments.
4. **Pause only under observed ownership.** Inspect root and profile pause state.
   Never overwrite a pre-existing pause. With no competing operator, engage the
   native root pause with a unique maintenance reason and immediately record its
   exact state and file identity. Native pause has no ownership token/CAS; this
   sequence is cooperative, not race-proof. Ambiguity is a stop, not permission
   to overwrite or resume. Preserve every unrelated profile pause.
5. **Drain before code/dependency changes.** Pause stops new work, not existing
   work. Require two consecutive settled observations: no running Kanban work,
   direct worker/process descendants or owned runtime activity; control-socket
   active_work explicitly idle and every expected service accounted for. Missing
   or failed inventory is unknown, never idle. Do not force-kill product workers.
   Inability to prove drain blocks the update.
6. **Back up and apply only the rehearsed transition.** Protect exact Git/config
   source baselines, service definitions, profile state and online integrity-checked
   SQLite backups, including external/symlinked state excluded by native backup.
   Keep secrets owner-only, outside Git. Confirm restoration/recovery access before
   mutation. Recheck snapshot, bundle protection, source configuration and native
   admission under the maintenance lock. Invoke the official updater only through
   the rehearsed immutable transport. Do not bypass native restrictions or invent
   a force flag. If required Default-last activation cannot be preserved by the
   chosen native path, stop and resolve that conflict before applying.
7. **Observe completion, then owned resume.** Require updater success, a fresh
   receipt attributable to this PID/time/invocation (not just latest.json), exact
   final SHA, expected persistent source, and every replacement process running
   the approved revision and healthy. Verify profile/config/approval preservation
   and document intended migrations. Perform bounded channel canaries. Only when
   safe and the pause still demonstrably belongs to this operation may Default
   explicitly resume; verify root and every profile pause state afterward. Native
   resume removes all sentinels visible to its invocation, so never run it blindly.
8. **Failure is explicit recovery.** Preserve logs, receipts, backups and pause
   evidence. Report unchanged, partially applied or applied-but-unverified accurately.
   Never unconditionally resume in finally, or assume native syntax rollback
   restores dependencies/migrations/state. Keep the fleet fenced if safety or
   pause ownership is uncertain and use the approved recovery procedure. Do not
   promise unattended recovery; an operator must reconcile terminal failures.

## Evidence and limits

The workflow tests use real disposable Git repositories, merges, bundles and
fetches, plus the public verification CLI. They run through the canonical test
runner with network unshared, live homes hidden and dependencies read-only.
Read-only verifier tests compare actual bytes/modes, including index/config/refs,
on both success and refusal. These are **workflow tests, not rollout tests**.

The original wrong-source test moved only main and missed the transport bug.
Moving the actual release ref produced 1 failed / 8 passed; the corrected rewrite
passed all 9. Adding verifier acceptance tests before implementation produced
15 failed / 9 passed; implementing verification passed all 24. Preserve those
receipts rather than presenting a clean first attempt.

No merge, deployment, source switch, pause/resume, service restart or full-state
rollback has been performed by this workflow. The cooldown PR remains a separate
change; a completed operator tool does not make that runtime release ready.
