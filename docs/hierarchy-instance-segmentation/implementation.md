# Hierarchy-aware instance segmentation: path to PR

## Status

Planning started 2026-08-11. Repository inspection and the preservation
checkpoint are complete. PR rebasing and PR creation remain intentionally
deferred.

Checkpoint commits pushed 2026-08-11:

- `ai-service`: `d2378ec`
- `backend`: `b159ea2`
- `frontend-react`: `a6f2fe9`
- `iquana-toolbox`: existing pushed contract commit `c2be767`
- `iquana-service-core`: existing checkpoint branch advanced to `6866915`

## Objective

Turn the current hierarchy-aware instance-segmentation implementation into a
set of reviewable, reproducible pull requests after the official repositories
have advanced. Preserve the local work, rebase it onto the correct current
upstream bases, remove runtime/generated noise, synchronize the shared toolbox
contract and dependency pins, and verify the complete training-to-inference
flow from a clean checkout.

## Repository and dependency map

| Repository | Local feature branch | Current PR base to confirm | Official remote in this workspace |
| --- | --- | --- | --- |
| \`iquana-toolbox\` | \`feat/hierarchy-aware-instance-training-contract\` | \`main\` | \`origin\` |
| \`ai-service\` | \`feat/hierarchy-aware-instance-training-ai\` | \`main\` | \`upstream\` |
| \`backend\` | \`feat/hierarchy-aware-instance-training-backend\` | \`dev\` | \`upstream\` |
| \`frontend-react\` | \`feat/hierarchy-aware-instance-training-frontend\` | \`dev\` | \`upstream\` |
| \`iquana-service-core\` | no hierarchy feature branch | only if required | \`upstream\` |

The branch/base choices are inferred from the current local tracking refs and
must be confirmed against the target repository before rebasing. The feature
branches are on personal remotes, so a rebase will require
\`--force-with-lease\` when they are pushed again.

## Acceptance criteria

- Every intended local feature change is preserved in a named checkpoint before
  any rebase or conflict resolution.
- Each PR branch is based on the latest official target branch and has no
  accidental generated files, caches, local databases, or unrelated changes.
- The toolbox hierarchy contract is reviewable first, and consumers use a
  stable official merged commit/tag or an explicitly documented temporary
  coordination pin rather than a local editable path.
- AI service, backend, and frontend branches compile/test against the final
  contract and current upstream APIs.
- Focused tests, full repository checks where practical, and a clean-checkout
  dependency/install check pass.
- The real CPU training smoke test, MLflow artifact reload, hierarchy label
  mapping, cancellation/failure lifecycle, and frontend trigger flow are
  either verified or explicitly listed as environment-dependent.
- Linked PRs are opened in dependency order with changed files, test results,
  rollout assumptions, and cross-repository dependencies documented.

## Scope boundary

The intended feature scope is the hierarchy-aware instance-segmentation
training, prediction, persistence, lifecycle, and UI integration recorded in
\`CHANGELOG_INSTANCE_SEGMENTATION.md\` and \`PR_HANDOFF.md\`.

Do not include \`ai-service/mlflow.db\`, \`backend/mlruns/\`, Python bytecode,
\`frontend-react/src/setupProxy.js\`, or unrelated \`iquana-service-core\` local
environment changes. Keep a real core change in a separate branch/PR.

## Risks and dependencies

- The working trees in \`ai-service\`, \`backend\`, and \`frontend-react\` contain
  tracked edits plus untracked runtime/test files. Rebasing a dirty tree is
  unsafe until the intended edits are checkpointed and the rest is isolated.
- The official remotes use different names (\`origin\` is official for the
  toolbox; \`upstream\` is official for the other active feature repos).
- The toolbox contract is a dependency of the service repos. A personal-fork
  Git revision is useful for coordination but should not be the final release
  dependency unless maintainers explicitly accept it.
- Rebase rewrites commit IDs. Push only with \`--force-with-lease\`, after a
  range-diff and test review.
- Upstream may have changed the same API, model-registry, training, or
  annotation files. Resolve behavior deliberately; do not accept “ours” or
  “theirs” for an entire file without checking the contract.
- Full validation depends on MLflow, Redis/Celery, model weights, and possibly
  GPU/CPU memory. Separate code failures from unavailable infrastructure.

## Phase map

1. **Checkpoint and scope freeze** — preserve all intended work and isolate
   generated/unrelated files. Approval gate before history changes.
2. **Refresh official bases** — fetch the official remotes, confirm each PR
   target branch, and record incoming commits.
3. **Prepare toolbox PR** — rebase the contract/MLflow changes onto official
   \`main\`, verify tests, and establish the dependency revision for consumers.
4. **Prepare AI-service PR** — rebase onto official \`main\`, resolve model and
   training lifecycle conflicts, update the toolbox pin, and verify training,
   artifact reload, and lifecycle tests.
5. **Prepare backend PR** — rebase onto official \`dev\`, resolve gateway,
   hierarchy export, prediction replacement, and model-registry conflicts,
   then validate route and integration behavior.
6. **Prepare frontend PR** — rebase onto official \`dev\`, resolve workspace and
   training UX changes, and run tests/lint/build. Revisit service-core only if
   the final dependency contract requires it.
7. **Clean-checkout gate and PR handoff** — verify the four branches together,
   push with lease protection, open linked PRs in dependency order, and record
   remaining environment-dependent checks.

## Definition of done

The four active feature branches are clean, based on current official targets,
and contain only the hierarchy instance-segmentation scope. Their dependency
pins are reproducible from a fresh checkout; the shared contract PR is linked
from the service PRs; the focused/full checks and the end-to-end smoke path
have recorded results; and the PR descriptions explain merge order, test
commands, known limitations, and any temporary coordination pin.
