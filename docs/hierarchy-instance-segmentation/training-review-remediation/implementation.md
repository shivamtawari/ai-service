# Hierarchy-Aware Instance Segmentation: PR Remediation and Saved Models

## Status

- Planning status: corrected after the 2026-08-10 same-image regression analysis and ready for the next fix agent.
- Implementation status: Phase 7 code complete with a local route-test harness hang; Phase 8 merge behavior complete; Phase 9 authorization complete but hierarchy write-safety partial; Phase 10 is partial with dataset-reactive inference catalog loading and stale-response protection complete; the remaining saved-model CTA/state items stay open; the Phase 11 shared toolbox dependency pin is complete across backend, AI service, and service core at `c2be767bd475d56ff8af7de6cfabe9c202ae3a6f`, while other repository-readiness checks remain separate; Phase 12 is reduced to a focused smoke gate.
- Existing feature status: **not PR-ready**. The saved-model plumbing works, but a same-image overfit regression must be isolated before adding any dataset-quality or validation machinery.
- Scope rule: implementation agents must preserve unrelated local changes and update the phase records in `implementation_details.md` as work proceeds.
- Source of truth: where an old phase completion record conflicts with the corrective review, the corrective phases and acceptance criteria in the two plan documents take precedence.
- Review rule: after every implementation review, append the verified findings, missing tests, and resulting phase-status changes to both plan documents before handing work back to an agent.

## Preservation Checkpoint — 2026-08-11

The PR was intentionally deferred, but the hierarchy-aware implementation and
its dependency state have been saved to the personal fork branches. These are
checkpoint commits; they have not been rebased onto newer official branches.

| Repository | Branch | Latest checkpoint | Remote result |
| --- | --- | --- | --- |
| `ai-service` | `feat/hierarchy-aware-instance-training-ai` | `237bea7` | pushed to `origin` |
| `backend` | `feat/hierarchy-aware-instance-training-backend` | `b159ea2` | pushed to `origin` |
| `frontend-react` | `feat/hierarchy-aware-instance-training-frontend` | `a6f2fe9` | pushed to `origin` |
| `iquana-toolbox` | `feat/hierarchy-aware-instance-training-contract` | `c2be767` | already pushed to `personal` |
| `iquana-service-core` | `checkpoint/instance-segmentation-2026-07-31` | `6866915` | pushed to `origin` |

The canonical implementation plans are also copied into
`ai-service/docs/hierarchy-instance-segmentation/` for preservation in the
fork. Runtime/generated files remain intentionally excluded: `mlflow.db`,
`app.db`, `backend/mlruns/`, `frontend-react/src/setupProxy.js`, and Python
bytecode.

Checkpoint verification:

- Toolbox focused tests: 29 passed.
- Frontend tests: 8 suites and 37 tests passed; lint had 0 errors and 36 warnings; the production build completed with warnings.
- AI-service tests were not completed because the environment could not install the dev dependency set (`ipykernel`) after a DNS/network failure.
- Backend focused tests hung without output and were interrupted with exit 130; no backend pass is claimed.

The next phase remains upstream refresh/rebase, followed by full validation and
PR preparation in dependency order. The checkpoint itself is complete.

## Objective

Make the hierarchy-aware instance-segmentation work safe to merge and useful end to end:

1. Correct the production, security, durability, hierarchy, and release blockers found in review.
2. Persist each successful custom training run as a distinct immutable MLflow registered model.
3. Expose only dataset-compatible trained models in the instance-segmentation selector.
4. Keep trainable base models separate from inference-ready custom models.
5. Preserve old trained models when a new run completes, while giving each model an exact, stable identity.

## Non-Goals

- A general-purpose model-management UI with rename, archive, or delete operations.
- Cross-dataset model sharing or label remapping.
- Arbitrary rollback between versions under one trained-model identity. Each run creates a new identity instead.
- Reworking the hierarchy encoding beyond the existing `exclusive_hierarchy_v1` contract.
- Broad refactors unrelated to the reviewed feature.

## Scope Calibration — Avoid Over-Engineering

The fourth review intentionally reduces the merge gate. The PR must be functionally correct, secure at exposed boundaries, independently buildable, and useful for selecting a saved model. It does **not** need a new general workflow engine or exhaustive infrastructure-hardening project.

### Required before PR

1. Fix the internally inconsistent MLflow alias contract and make its focused test pass.
2. Enforce dataset-scoped authorization for exposed training task/run operations.
3. Prevent cross-dataset/invalid-label prediction writes and preserve nested descendants in the hierarchy-aware Patch path.
4. Complete the minimum saved-model UI path: expose output identity, refresh the active dataset's model catalog, select the canonical key, and avoid stale cross-dataset responses.
5. Replace editable sibling toolbox dependencies with one immutable shared revision/release; exclude generated/protected local files.
6. Keep focused changed-area tests and `git diff --check` green. Diagnose the backend route-test harness hang sufficiently to distinguish environment/fixture failure from feature failure.
7. Run one focused API/service smoke: submit request -> build publication metadata -> register model -> discover it for the same dataset. A full browser automation stack is not required for this PR.

### Follow-up hardening unless current deployment requires it

- Full lease/heartbeat/stale-job reconciliation and exhaustive worker-loss fault injection.
- Multi-GPU scheduling/orchestration beyond documenting a one-worker deployment command.
- Automated export retention/orphan cleanup.
- Supporting more than 254 instances per image; document the current limit and create a follow-up unless real in-scope datasets exceed it.
- Full legacy-run-history merging, hierarchy focus/refinement repair, and capability negotiation beyond the changed saved-model path.
- Two-run browser E2E, every fault-point matrix, and production-like distributed infrastructure in CI.

Do not repeatedly reopen a verified item without new contradictory evidence. Reviewers should report only new regressions, unresolved merge blockers, and the status of explicitly deferred follow-ups.

## PR Blockers to Resolve

### Phase 7 contract gate

- The shared request now defines `selected_label_ids` and `enable_hierarchy`, and string `trained_by` plus canonical model tags are implemented.
- The backend now passes the selected/exported label IDs, closing the empty published label-scope defect in request construction.
- Hierarchy mode is now derived from selected label parent/child relationships.
- The integration-shaped test covers request serialization and temporary MLflow discovery but manually reconstructs worker metadata; this is sufficient as supporting evidence, not a reason to build a larger framework.
- The claimed backend route verification cannot currently be reproduced: `tests/test_instance_seg_training_route.py` times out without completing.

### P1: publication integrity and durable lifecycle

- Submission stores an immutable source version and the worker now loads that version.
- Python and Redis cancellation now both reject `REGISTERING` cancellation.
- Publication now assigns the supported `active` alias and propagates alias errors; toolbox tests pass.
- Backend metadata lookup and AI instance-segmentation inference/preload now try `active` first and preserve `latest` fallback for base/legacy models.
- The focused `active` publication/consumption contract is complete in code; keep source-base alias resolution separate.
- Worker redelivery/reconciliation and GPU orchestration are follow-up hardening under the calibrated scope unless required by the current deployment topology.

### P1: authorization, prediction integrity, and catalogs

- Dataset-scoped inference catalog access now requires explicit `dataset_id` and `DATASET_READ`.
- Training status, run detail, stream, and cancellation now fail closed when dataset ownership metadata is absent and enforce dataset-scoped `AI_TRAIN`.
- Focused tests were added, but the backend route-test module still hangs in this reviewer environment before producing results.
- Prediction labels are now checked recursively against model scope, but trained models with missing `dataset_id` are accepted and the model's complete label set is not validated against current target-dataset labels/hierarchy before writes.
- Patch mode now preserves a novel predicted descendant beneath a duplicate predicted parent by attaching it to the matching existing parent.

### P1: saved-model user workflow

- Durable status/history enrichment drops the output registry key, version, alias, and URI.
- The training success UI does not show a saved-model identity or provide the required `Use in annotation` action.
- Annotation model loading still runs only on initial mount when the list is empty; dataset changes, stale favorites, and late asynchronous responses can retain or restore a model from the previous dataset.
- A failed cancellation still invalidates the live stream and does not reconnect streaming or polling.

### P2: remaining correctness and release readiness

- Samples with more than 254 instances are rejected; the requirement is to encode more than 255 instances without assigning the ignore index to a real object.
- Backend per-label export preflight is implemented, but the route-level mixed-label/no-AI-dispatch regression needs deterministic test evidence.
- Serialized label validation still performs substring matching.
- Export retention/orphan cleanup and hierarchy focus/refinement repair remain incomplete.
- Historical dependency snapshot `e82362be6120853e944814a19d1cfae7f99592a6` used incompatible toolbox revision `bb26d0b7ee5a57fcc7592f29a77b9048e614059a`; that revision lacked `selected_label_ids`, `enable_hierarchy`, and the `active` publication contract used by this feature.
- Toolbox commit `c2be767bd475d56ff8af7de6cfabe9c202ae3a6f` is the compatible immutable revision and is reachable from the personal fork. It contains the hierarchy request fields, trained-model metadata parsing, publication result, canonical tags, and `active` alias behavior.
- Backend, AI service, and service-core declarations and lockfiles consistently pin `c2be767...`, contain no editable toolbox source, and pass `uv lock --check`. The shared dependency pinning blocker is closed.
- The current checkpoint pins backend, AI service, and service-core to compatible toolbox commit `c2be767...` from the personal fork. Before PR, replace that coordination pin with the official merged/released revision and verify clean checkouts.
- Historical toolbox/AI suite results remain recorded below; the 2026-08-11 checkpoint validation is the authoritative latest evidence: toolbox focused tests 29 passed, frontend 37 passed, AI tests blocked by missing dev dependencies/network, and backend focused tests hung.
- `git diff --check` is clean again in backend, AI service, and toolbox.

## Architecture Decisions

### 1. Publish one immutable registered model per successful run

Each submitted training job receives an output registry key before it is enqueued:

```text
mask2former-ds<dataset-id>-<task-uuid>
```

The exact normalized format may be centralized in the shared toolbox, but identity must include the dataset and full task UUID or an equivalently collision-resistant value. A user-entered run name is display metadata only and must never be used as the registry identity.

Each output key represents one trained artifact. Its created model version receives the `active` alias only within that unique key. Retraining creates a new key, so previous custom models remain loadable and selectable.

### 2. Separate training bases from trained inference models

Registry metadata must include `model_role`:

- `training_base`: selectable on the training page and never offered for inference.
- `trained`: selectable for inference only when its `dataset_id`, `label_ids`, task, encoding, and loadable alias are valid.

The shared base model remains a template. At submission, the service resolves and stores its exact version/URI so a queued job cannot silently switch bases if the base alias moves.

### 3. Make publication an explicit commit boundary

A training job may be cancelled while queued or training. Once it atomically enters `REGISTERING`, publication is non-cancellable. The worker must:

1. Register the unique output model.
2. Capture the exact created version.
3. Assign `active` to that version.
4. Verify the aliased model can be resolved.
5. Persist output identity on the durable job.
6. Mark the job `SUCCEEDED`.

Failure before this sequence completes must not report success. If registration partially succeeds, reconciliation must either finish the idempotent publication using the task-derived identity or mark the job failed with actionable diagnostics; it must not create a second identity.

### 4. Split model catalog responsibilities

- Add a training-base catalog endpoint for the training page.
- Make the instance-segmentation inference catalog dataset-scoped and return only inference-ready trained models.
- Preserve exact canonical registry keys through backend and frontend state; display names are labels, not identifiers.
- Batch-inference planning that uses the same registry service must be regression-tested before filtering behavior changes.

### 5. Enforce integrity recursively and at service boundaries

- Validate every prediction node, including all descendants, against the target dataset and model label contract before persistence.
- Treat each selected training label as requiring at least one eligible annotation.
- Preserve novel nested descendants in Patch mode even if their predicted root matches an existing object.
- Reserve/avoid the configured ignore index when assigning instance IDs.

### 6. Serialize shared resources and make artifacts immutable

- Export each job to a unique path and publish it atomically.
- Keep the export until the consuming job is terminal; clean it through an explicit retention policy.
- Route GPU training through a dedicated queue whose worker concurrency matches available accelerator capacity, initially one job per GPU.
- Use idempotent state claims, heartbeats, and stale-job reconciliation for queued, running, and registering states.

## Acceptance Criteria

### Build and dependency criteria

- Toolbox changes are committed, tested, and available through a reproducible immutable dependency reference or release.
- Backend and AI service no longer use editable sibling-directory dependencies.
- Clean isolated installs of all affected repositories succeed.
- Feature branches are reconciled with their upstream branches without losing upstream fixes.
- `git diff --check` is clean in all affected repositories.

### Saved custom model criteria

- Every successful training task creates exactly one unique trained-model registry key and records its exact model version, alias, and URI.
- Completing another run never overwrites or hides a previous run's model identity or metadata.
- The source base model version/URI is pinned at submission and recorded with the trained model.
- The resulting alias resolves and can be loaded before the job is marked successful.
- A successful run shows its saved-model identity in training history and offers a direct route to use it for that dataset's annotation workflow.
- The dataset's instance-segmentation selector lists the newly trained model after refresh and sends its canonical registry key when selected.
- Base/untrained models are absent from the inference selector and trained models are absent from the training-base selector.
- A trained model from dataset A is not visible or usable for dataset B.

### Lifecycle and concurrency criteria

- Duplicate task delivery is idempotent and does not fail solely because the durable job is already `RUNNING` or `REGISTERING`.
- Worker loss cannot leave a job permanently stuck; stale active jobs are retried safely or reach a terminal failure with a reason.
- Cancellation before publication is reflected consistently in durable state and UI state.
- Cancellation after registration begins returns a conflict/non-cancellable response and cannot produce a cancelled job with a published model.
- Concurrent submissions never share or overwrite an export artifact.
- Configured worker concurrency prevents multiple GPU-heavy training jobs from using one GPU simultaneously unless explicitly supported.

### Training and hierarchy criteria

- Preflight rejects every selected label that has zero eligible examples and returns per-label diagnostics.
- A one-image diagnostic can deliberately overfit and emit both selected mapped classes on that same image. This is a regression test, not a production-quality claim.
- The hierarchy-aware and pre-hierarchy paths are compared with identical random seed, epochs, learning rate, batch size, source checkpoint, input image, and confidence threshold before changing training policy.
- More than 255 instances are encoded without assigning the ignore index to a real instance.
- Trained-model metadata stores parsed label IDs and a segmentation mode/target encoding derived from the submitted dataset contract; hierarchical runs advertise `hierarchical` plus `exclusive_hierarchy_v1`.
- Label matching parses structured metadata and never uses substring checks.
- Replace and Patch preserve valid nested hierarchy; Patch retains novel descendants below duplicate roots.

### Prediction and security criteria

- Every prediction node is recursively validated before any annotation write occurs.
- Model label IDs must be a subset of the target dataset's labels.
- Cross-dataset or unauthorized job IDs cannot be read, streamed, cancelled, or deleted.
- Every training operation enforces dataset-scoped `AI_TRAIN` permission without leaking job details before authorization.

### Frontend and compatibility criteria

- Applying replacement hierarchy clears or repairs stale focus and refinement references.
- Cancel success updates both selected-run and history state; cancel failure restores status streaming or polling.
- History combines durable jobs and valid legacy MLflow runs without duplicate identities.
- Progress exposes a stable total epoch count or an explicit indeterminate state.
- Frontend Patch behavior is deployed only after the compatible backend, or guarded by an explicit capability check.
- Existing annotation, inference planning, flat segmentation, and non-training model selectors do not regress.

### Verification criteria

- Unit, contract, integration, and frontend tests cover each blocker and saved-model flow.
- Backend route tests that previously hung are made deterministic or their environment/fixture blocker is documented and resolved before PR approval.
- A clean end-to-end smoke run trains, publishes, discovers, selects, and invokes a custom model against the same dataset.

## Phase Map

| Phase | Outcome | Depends on |
| --- | --- | --- |
| 0. Baseline and branch hygiene | Reconciled branches, protected local work, reproducible failure/test baseline | None |
| 1. Shared toolbox contract and release | Typed registry publication result, aliases, model roles, and immutable dependency source | Phase 0 |
| 2. AI saved-model publication | Unique model per run, pinned base, verifiable alias, durable output identity | Phase 1 |
| 3. AI lifecycle and training correctness | Idempotent recovery, safe cancellation, GPU queue, strict labels and instance encoding | Phase 2 |
| 4. Backend integrity, authorization, and catalogs | Unique exports, recursive prediction checks, job authorization, split model catalogs | Phases 1-3 |
| 5. Frontend selection and state recovery | Dataset-scoped custom-model selector, saved-model CTA, hierarchy/failure state fixes | Phase 4 |
| 6. Cross-service verification and PR preparation | Clean installs, full regression suites, end-to-end smoke, rollout-ready PR set | Phases 0-5 |
| 7. Contract repair and publication discovery | Fix request/model metadata types and canonical registry tags; prove publication-to-catalog discovery | Reopens Phases 1-2 |
| 8. Publication atomicity and lifecycle recovery | Immutable base pin, verified alias, idempotent claims, stale reconciliation, cancellation boundary, GPU serialization | Phase 7 |
| 9. Backend security and hierarchy integrity | Dataset-authorized jobs, recursive validation, model/dataset checks, Patch descendant preservation, strict preflight | Phases 7-8 |
| 10. Frontend saved-model completion | Dataset-reactive catalog, canonical selection, saved output details, use-in-annotation CTA, cancellation/state repair | Phase 9 |
| 11. Dependency and repository readiness | Immutable toolbox dependency, upstream reconciliation, clean diffs and isolated installs | Phases 7-10 |
| 12. Integrated release gate | Full suites, fault injection, security tests, and two-run end-to-end smoke | Phases 7-11 |
| 13. Hierarchy training-data normalization | Summarize invalid geometry, explicitly normalize eligible parent/child masks for export, and exclude unsafe images without mutating annotations | Phase 9 |

Phases 2-4 may be implemented on separate service branches after the Phase 1 contract is frozen, but Phase 5 must target the final backend API contract and Phase 6 must verify the exact integrated revisions.

## Dependencies and Rollout Order

1. Reconcile and release/pin `iquana-toolbox`.
2. Merge/deploy AI service support for the shared contract and unique publication.
3. Merge/deploy backend authorization, catalog, export, and prediction changes.
4. Merge/deploy frontend behavior after backend compatibility is present.
5. Run the end-to-end smoke test using production-equivalent MLflow, Redis/Celery, and dataset storage.

If services are rolled independently, the backend must maintain backward-compatible response fields while the frontend migrates. Patch mode must remain disabled in the frontend until the backend advertises or operationally guarantees the new apply semantics.

## Risks and Mitigations

- **Partial MLflow publication:** derive identity from the task, persist publication checkpoints, and reconcile idempotently.
- **Existing registry entries lack `model_role`:** explicitly classify/migrate known base entries; do not infer that every legacy entry is inference-ready.
- **Large model catalog:** query by dataset/role server-side and paginate if required; do not download every model artifact to build a selector.
- **Upstream branch conflicts:** reconcile before feature edits and rerun the baseline tests after conflict resolution.
- **Shared model-registry filtering affects batch planning:** add contract tests for both interactive annotation and batch inference consumers.
- **Export retention leaks storage:** terminal cleanup plus a bounded orphan-retention job, with paths restricted to the training-export directory.
- **Stale frontend favorites:** validate stored canonical keys against the dataset catalog and fall back deterministically.
- **Mixed-version deployment:** preserve response compatibility and enforce backend-first rollout/capability gating.

## Definition of Done

- All acceptance criteria above are checked off with test or smoke-test evidence.
- Corrective phases 7-12 are complete; the historical phase 0-6 records alone do not satisfy this condition.
- No P0/P1 review finding remains open; accepted lower-priority deferrals are documented with owners.
- The shared request and model metadata schemas are exercised by the actual Celery publication function using a normal string username.
- A temporary real MLflow store proves that one published model is immediately discoverable through the backend dataset-scoped inference catalog.
- All affected repositories build from clean checkouts without sibling path assumptions.
- Every repository has a reviewable, scoped diff with no unrelated local files or generated databases included.
- Shared contracts and endpoint behavior are documented close to their schemas/routes.
- The phase boundary log in `implementation_details.md` records changed files, commands, outcomes, residual risks, and the recommended next phase.
- The final PR descriptions state dependency/merge order and link the corresponding toolbox revision.

### Hierarchy normalization criteria

- Strict submission scans every eligible image and returns one structured summary instead of stopping at the first invalid contour.
- The summary identifies affected image IDs/file names, automatically adjustable child counts, unsafe child counts, and sibling-overlap counts.
- An explicit user-approved retry may expand a training parent mask by unioning children that are at least 50% contained; source annotations remain unchanged.
- Any image containing a child below 50% containment is excluded from that training export and listed in the response/run metadata.
- Sibling overlaps are never resolved silently. The chosen deterministic ownership policy must be explicit in the confirmation UI and covered by focused encoder/export tests.
- The frontend presents one confirmation summary and retries with the selected normalization policy; cancellation leaves the dataset and training state unchanged.
