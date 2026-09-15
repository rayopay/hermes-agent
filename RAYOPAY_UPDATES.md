# Rayopay reviewed-update workflow — development checkpoint

This tooling is **not deployed and is not a complete release pipeline**.
Preparation and exact-version Git transport are implemented. The deployment
controller is not implemented; pause/resume ownership and failure recovery need
a scope decision. No merge, source switch, restart or installation is authorized
by running preparation.

## Working commands

Run `python3 scripts/rayopay_release.py --help` for the complete interface.

- `prepare`: requires exact upstream/downstream SHAs, a clean fork checkout, a
  separate clean installed checkout, and a new operation directory outside both.
  It fetches the pinned upstream into the **maintenance repository only**, makes
  a retained detached candidate worktree, and merges without changing either
  input checkout. Conflicts are preserved and produce a blocked receipt. It
  refuses a candidate which would omit the installed version.
- `status`: reads an existing preparation receipt.
- `bundle`: exports a prerequisite Git bundle for the exact prepared candidate.
  It does not run tests, grant approval, publish a branch or install anything.

The `prepare` result is deliberately `prepared-not-qualified`. Actual Hermes
regression/lifecycle qualification, independent review, release approval and
rollout are separate obligations. Test failures cannot be waived by creating a
bundle. There is no automatic GitHub publication or merge command.

## Exact-version transport

`pinned_git_env` uses Git's invocation-scoped `url.BUNDLE_PATH.insteadOf` rule.
It refuses inherited Git-config injection and does not edit persistent remotes.
A simple `remote.origin.url` override is unsafe here: remote URLs accumulate,
and the persistent first URL can remain effective.

The behavioral test moves the **same release ref** on the remote after bundle
creation, checks resolved URL selection, and performs a real fetch from the
bundle. It requires the approved SHA even though the remote now advertises a
different SHA, and verifies that the persistent origin remains unchanged.

A bundle is not a deployment lock. Before any future application, require its
hash and advertised ref, target prerequisites, protected transport path and
source identity to remain exact. A non-main release branch avoids the native
updater's optional upstream-sync/push path. A clean initial release-branch
baseline must be rehearsed: native creation directly at the new candidate can
select the updater's already-up-to-date path instead of its normal update path.

## Deployment decision still open

Recommended small first version: this tool prepares the release and performs
read-only checks; Default conducts a separately approved, explicitly observed
maintenance window, including pause/resume. The alternative is a larger reviewed
ownership/recovery layer. No selection was received before the clarification
prompt timed out; neither path has been silently implemented or authorized.

Native ESTOP overwrites a prior pause and native resume removes visible pause
sentinels without an ownership/CAS API. Do not blindly clear a pause in a generic
failure handler. Native inventory alone is not proof that all work has drained.
The updater's own restart drain occurs after source/dependency mutation, so it
cannot substitute for pre-update fleet settlement. Never clear delegated-context
markers or start an indirect writer to bypass a native authority refusal.

The native updater remains responsible for dependency refresh, migrations,
maintenance and its documented restart/receipt mechanisms. It does not supply a
general full-state rollback command. A future deployment requires backups,
separately reviewed recovery, independent running-version/health checks and an
explicit update-source transition. A post-update SHA mismatch is detection,
not permission to install an unapproved revision.

## Tests at this checkpoint

Canonical `scripts/run_tests.sh tests/scripts/test_rayopay_release.py`:
**9 passed, 0 failed, exit 0**, in a disposable network-isolated source snapshot
with live homes hidden and the prepared dependency environment read-only.
These are real disposable Git workflow tests, **not native Hermes rollout tests**.

The original transport fixture moved only the default branch and missed the
wrong-source bug. Strengthening it to move the actual release ref produced
**1 failed / 8 passed** with the original URL override. The corrected rewrite
then passed all 9 tests. Earlier sandbox setup and runner-duration-cache failures
are retained separately; they are not clean test invocations.

The immutable cooldown candidate and PR #3 are untouched by this workflow branch.
Do not merge this development checkpoint or change the live update source merely
because these orchestration tests pass.
