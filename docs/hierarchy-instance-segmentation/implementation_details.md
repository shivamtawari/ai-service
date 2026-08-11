# Hierarchy-aware instance segmentation: implementation details

## Current inventory (2026-08-11)

The workspace contains independent Git repositories. The root directory is a
container for the repositories and planning notes, not one PR checkout.

| Repository | Observed state | Action |
| --- | --- | --- |
| \`ai-service\` | \`1bfc519\` on the hierarchy feature branch; tracked edits in routes/tasks/dataset/tests/dependencies; untracked \`mlflow.db\` | checkpoint intended edits; exclude the MLflow database |
| \`iquana-toolbox\` | clean at \`c2be767\`; feature branch tracks the personal fork; local \`main\` and official \`origin/main\` have diverged | rebase the feature branch onto refreshed official \`origin/main\` |
| \`backend\` | \`f5e00c3\`; tracked hierarchy edits; large untracked \`mlruns/\` artifact tree | checkpoint intended edits; exclude MLflow artifacts |
| \`frontend-react\` | \`b987560\`; tracked hierarchy/training UI edits; untracked focused tests and \`src/setupProxy.js\` | include the two feature tests after review; exclude \`setupProxy.js\` |
| \`iquana-service-core\` | on \`main\` with bytecode deletions/additions and \`pyproject.toml\`/\`uv.lock\` local changes | do not mix into the feature PR unless a concrete shared dependency change is required |

The existing \`PR_HANDOFF.md\` reports the lifecycle/hierarchy work through its
recorded Phase 4 boundary and recommends the persistence/replacement phase;
the current branches and dirty edits still require a final scope and
verification pass before PR creation.

## Phase 1 — checkpoint and scope freeze

Status: pending execution.

Before fetching, rebasing, cleaning, or regenerating locks:

1. In each active feature repository, record \`git status --short --branch\`, the
   current commit, the configured remotes, and the target PR base.
2. Create a local checkpoint branch or an explicit WIP checkpoint commit from
   the intended changes. A safe pattern is to create a checkpoint branch,
   explicitly add only the feature files, commit there, and create the later
   preparation branch from that checkpoint. This keeps the old feature ref as a
   second recovery point.
3. For the current workspace, review these tracked edits as likely feature
   scope:

   - \`ai-service/app/routes/instance_seg.py\`
   - \`ai-service/app/routes/models.py\`
   - \`ai-service/app/routes/training.py\`
   - \`ai-service/app/tasks.py\`
   - \`ai-service/app/training_jobs.py\`
   - \`ai-service/models/mask2former_dataset.py\`
   - \`ai-service/tests/test_training_jobs.py\`
   - \`backend/app/routes/services/instance_seg_router.py\`
   - \`backend/app/services/instance_prediction_application.py\`
   - \`backend/app/services/instance_segmentation_training.py\`
   - \`backend/app/services/model_registry.py\`
   - their related tests and dependency declarations
   - the frontend tracked files shown by \`git diff --stat\`

   Review each file against the changelog before staging; the list is a
   starting point, not permission to stage every local edit.
4. Include \`frontend-react/src/components/modelTraining/trainingPage/ProgressPanel.test.jsx\`
   and \`frontend-react/src/stores/slices/modelsSlice.test.js\` only if they are
   part of this feature and pass review. Keep
   \`frontend-react/src/setupProxy.js\` out of the PR.
5. Do not use \`git clean\`, broad \`git add .\`, \`git reset --hard\`, or a broad
   recursive delete to make a tree look clean. Generated databases/artifacts
   may be moved aside only after confirming they are disposable; otherwise
   leave them untracked and use explicit staging.

Checkpoint evidence to record:

- old feature branch name and commit;
- checkpoint branch/commit;
- exact staged file list;
- files intentionally left out;
- \`git diff --check\` result.

## Phase 2 — refresh official bases

Status: pending Phase 1.

Do not run \`git pull\` on the dirty feature branches. After the checkpoint,
fetch only the official remote in each repository:

~~~bash
# ai-service, backend, frontend-react
git fetch upstream --prune

# iquana-toolbox (origin is the official Iquana-tool repository here)
git fetch origin --prune

# iquana-service-core, only if it becomes part of the dependency work
git fetch upstream --prune
~~~

Confirm the exact target branch with the official repository/PR convention.
The current local evidence suggests \`main\` for the toolbox and AI service and
\`dev\` for backend and frontend, but this must not be assumed after fetching.
Record the new base SHA and inspect incoming work before rebasing:

~~~bash
git log --oneline --decorate <official>/<base>..HEAD
git log --oneline --decorate HEAD..<official>/<base>
git diff --stat <official>/<base>...HEAD
~~~

If the incoming branch contains an overlapping hierarchy implementation, stop
and compare behavior/commit ownership before replaying it. The result may be a
smaller cherry-pick rather than a blind full rebase.

## Phase 3 — toolbox contract PR

Status: pending Phase 2.

Repository: \`iquana-toolbox\`; official remote: \`origin\`; target: likely
\`origin/main\`.

1. Rebase the preparation branch onto refreshed official \`origin/main\`.
2. Resolve conflicts in model metadata, training request schemas, and MLflow
   registry behavior while preserving the hierarchy contract and the DINOv3
   backbone required by \`ai-service\`.
3. Keep only the contract/registry changes represented by \`c2be767\` and any
   necessary conflict-resolution updates.
4. Run the representation and MLflow registry tests, then the toolbox suite.
5. Push the rewritten branch to \`personal\` with \`--force-with-lease\` only after
   reviewing the range-diff.
6. Open the toolbox PR first. Prefer merging/releasing it before finalizing
   consumer pins. If parallel review is necessary, document the temporary
   personal-fork commit pin in dependent draft PRs and replace it with the
   official merged tag/SHA before marking them ready.

Suggested checks:

~~~bash
uv run pytest -q tests/test_representation_contract.py tests/test_mlflow_registry.py
uv run pytest -q
~~~

## Phase 4 — AI-service PR

Status: pending toolbox contract decision.

Repository: \`ai-service\`; official remote: \`upstream\`; target: likely
\`upstream/main\`.

1. Rebase the checkpoint/preparation branch onto the refreshed official base.
2. Resolve conflicts in model discovery, latest-model resolution, training
   routes/tasks, lifecycle state, COCO loading, and tests one behavior at a
   time. Preserve the current cancellation and registration boundary rather
   than reintroducing hard worker termination.
3. Update \`pyproject.toml\` and \`uv.lock\` to the final official toolbox
   revision. Do not leave the local editable path or an accidental personal
   fork pin in the release-ready branch.
4. Regenerate the lockfile from the final declaration once; review the lock
   diff for unrelated dependency churn.
5. Run focused tests first, then the full suite:

~~~bash
uv run --extra dev pytest -q \
  tests/test_training_jobs.py \
  tests/test_mask2former.py \
  tests/test_mask2former_integration.py
uv run pytest tests -v
~~~

6. Verify a real tiny CPU training step, finite loss, MLflow artifact package,
   fresh-process reload after removing the worker-local checkpoint, sparse
   database-label mapping, and terminal cancellation/failure behavior.

## Phase 5 — backend PR

Status: pending AI-service contract/API alignment.

Repository: \`backend\`; official remote: \`upstream\`; target: likely
\`upstream/dev\`.

1. Rebase onto the refreshed official \`dev\` after the API contract is stable.
2. Resolve conflicts in the instance-segmentation router, training service,
   model registry, hierarchy COCO export, and transactional prediction
   replacement. Check that upstream authorization and workspace changes remain
   intact.
3. Pin the final official toolbox revision and regenerate \`uv.lock\` only after
   \`pyproject.toml\` is final.
4. Exclude \`mlruns/\` and any model artifacts from the index.
5. Run focused tests, then the backend suite:

~~~bash
uv run pytest -q \
  tests/test_instance_segmentation_hierarchy_export.py \
  tests/test_instance_seg_training_route.py \
  tests/test_instance_prediction_application.py
uv run pytest tests -q
~~~

Verify selected-label filtering, transitive hierarchy handling, native image
dimensions, authorization, rollback on failed inference, post-commit side
effects, and authoritative training status/SSE behavior.

## Phase 6 — frontend PR and optional core work

Status: pending backend endpoint shape.

Repository: \`frontend-react\`; official remote: \`upstream\`; target: likely
\`upstream/dev\`.

1. Rebase onto the refreshed official \`dev\` after backend endpoint behavior is
   known.
2. Resolve workspace, model discovery, training progress, cancellation, and
   annotation refresh conflicts without dropping upstream workspace redesign
   work.
3. Add the two focused tests only if they are feature-owned; keep
   \`src/setupProxy.js\` out of the PR.
4. Run:

~~~bash
CI=true npm test -- --watch=false
npm run lint
npm run build
~~~

5. Perform the manual flow: training model discovery, selected labels,
   progress/cancel/terminal state, restart/reload, annotation Run button,
   keyboard shortcut \`3\`, warning modal, hierarchy parent/child insertion, and
   preservation of unrelated contours.

\`iquana-service-core\` stays out of this sequence unless a clean install proves
that the final toolbox/service contract requires a core code change. If it
does, create a separate feature branch/PR, exclude all \`__pycache__\` files,
and place that PR before the service consumer that needs it.

## Phase 7 — clean-checkout and PR handoff

Status: pending Phases 3–6.

For every preparation branch:

~~~bash
git status --short --branch
git diff --check
git diff --stat <official>/<base>...HEAD
git diff --name-only <official>/<base>...HEAD
~~~

Compare the rebased series with the checkpoint using \`git range-diff\` so no
feature commit disappeared and no unrelated commit entered. Check the staged
tree for databases, MLflow artifact directories, bytecode, local proxy files,
and editable dependency paths.

Push only after tests and range-diff review:

~~~bash
git push --force-with-lease <personal-remote> <feature-branch>
~~~

Open/link PRs in this order:

1. \`iquana-toolbox\` → official \`main\`;
2. \`ai-service\` → official \`main\`;
3. \`backend\` → official \`dev\`;
4. \`frontend-react\` → official \`dev\`.

Each description should contain the problem, cross-repo contract, behavioral
changes, explicit non-goals, exact test commands/results, integration
environment requirements, and links to prerequisite/dependent PRs. Keep
dependent PRs draft until the toolbox revision and API assumptions are clear.

## Conflict-resolution protocol

For each conflict:

1. Read the base version, feature version, and surrounding callers/tests.
2. Identify the contract being preserved: model registry URI/version,
   hierarchy encoding, label mapping, lifecycle state, API response, or UI
   state.
3. Resolve the smallest region, add/update a regression test, and run the
   narrowest relevant test before continuing.
4. Use \`git rebase --abort\` if the chosen replay is wrong. If the branch has
   become too tangled, create a new branch from the fresh official base and
   cherry-pick the reviewed feature commits in dependency order.

Never resolve a whole file by taking one side without checking the callers and
tests. Never use destructive reset/clean commands as a conflict strategy.

## Phase-boundary report template

At the end of each repository phase, record:

- base remote and exact base SHA;
- final branch SHA and changed-file list;
- conflicts and behavioral decisions;
- dependency source/revision;
- focused/full test commands and results;
- whether real MLflow/CPU/GPU/integration validation ran;
- remaining risks and the next dependent phase.
