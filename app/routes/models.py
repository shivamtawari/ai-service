"""Task-filtered model-registry routes for the unified service.

This supersedes ``iquana_service_core.routers.models.build_model_routers`` for
the merged service in two ways:

* It filters by the *filter-safe per-task* tag (``task_<name>`` == "true") rather
  than the legacy single ``task`` tag, so a model that advertises several tasks
  (e.g. SAM 3) shows up under every surface it serves -- not only its primary one.
* It reads each model's full ``model_info`` from the logged model's **artifact
  metadata**, not by rebuilding it from the registered-model tags. Tags only carry
  the filterable subset (task/status/...), so ``get_model_infos_via_tags`` ->
  ``parse_tags_to_model_info`` fails on the required ``ModelInfo`` fields
  (registry_key/name/description/usage_tip). ``register_model`` stores the full
  ``ModelInfo.model_dump()`` as the logged model's metadata, which is the lossless
  source of truth -- the same approach the backend gateway uses.

To be upstreamed into service-core in Phase 1; kept local for now so the shared
repo stays untouched mid-migration.
"""
from logging import getLogger
from typing import Tuple

import mlflow
from fastapi import APIRouter
from iquana_toolbox.mlflow import MLFlowModelRegistry

from models.base import get_task
from paths import MLFLOW_URL

logger = getLogger(__name__)


def _registered_names_for_task(
    registry: MLFlowModelRegistry,
    task_name: str,
    *,
    ready_only: bool,
    model_role: str | None = None,
    dataset_id: int | None = None,
) -> list[str]:
    """Registered model names advertising ``task_name`` (optionally filtered by ready status, role, and dataset_id)."""
    task_tag = get_task(task_name).tag_key
    status = {"status": "ready"} if ready_only else {}
    by_name: dict[str, None] = {}
    for tags in ({task_tag: "true", **status}, {"task": task_name, **status}):
        if model_role:
            tags["model_role"] = model_role
        if dataset_id is not None:
            tags["dataset_id"] = str(dataset_id)
        filter_string = " AND ".join(f"tags.{key} = '{value}'" for key, value in tags.items())
        try:
            for model in registry.client.search_registered_models(filter_string=filter_string):
                by_name[model.name] = None
        except Exception as e:
            logger.warning(f"Error searching models with filter '{filter_string}': {e}")
    return list(by_name)


def _full_model_info(registry_key: str, default_alias: str = "active") -> dict:
    """A model's complete ``model_info`` from its logged artifact metadata."""
    for target in (f"models:/{registry_key}@{default_alias}", f"models:/{registry_key}@latest"):
        try:
            info = mlflow.models.get_model_info(target)
            if info.metadata:
                return info.metadata
        except Exception:
            continue
    logger.warning("Model '%s' has no artifact metadata; returning stub.", registry_key)
    return {"registry_key": registry_key, "name": registry_key}


def build_task_model_routers(
    registry: MLFlowModelRegistry, task_name: str
) -> Tuple[APIRouter, APIRouter]:
    """Build the (public, session) model routers for one task surface."""
    router = APIRouter()
    session_router = APIRouter(prefix="/annotation_session", tags=["annotation_session"])

    def _list(ready_only: bool, model_role: str | None = None, dataset_id: int | None = None) -> list[dict]:
        mlflow.set_tracking_uri(MLFLOW_URL)
        names = _registered_names_for_task(
            registry, task_name, ready_only=ready_only, model_role=model_role, dataset_id=dataset_id
        )
        return [_full_model_info(name) for name in names]

    @router.get("/models/all", tags=["models"])
    async def list_models(
        model_role: str | None = None,
        dataset_id: int | None = None,
    ):
        """List models advertising this task with optional role and dataset filter."""
        models = _list(ready_only=False, model_role=model_role, dataset_id=dataset_id)
        return {
            "success": True,
            "message": f"Retrieved {len(models)} models.",
            "result": models,
        }

    @router.get("/models/all/available", tags=["models"])
    async def list_available_models(
        model_role: str | None = None,
        dataset_id: int | None = None,
    ):
        """List ready models with optional role and dataset filter."""
        models = _list(ready_only=True, model_role=model_role, dataset_id=dataset_id)
        return {
            "success": True,
            "message": f"Retrieved {len(models)} available models.",
            "result": models,
        }


    @router.get("/models/{model_registry_key}", tags=["models"])
    async def get_model(model_registry_key: str):
        """Return full model info for a single model (from artifact metadata)."""
        mlflow.set_tracking_uri(MLFLOW_URL)
        return {
            "success": True,
            "message": "Retrieved model information.",
            "result": _full_model_info(model_registry_key),
        }

    @session_router.get("/models/{model_registry_key}/preload", tags=["models"])
    async def preload_model(model_registry_key: str, user_id: str):
        """Warm a model into the registry cache at the start of a session."""
        try:
            registry.get_model_by_alias(model_registry_key, "active")
        except Exception:
            registry.get_model_by_alias(model_registry_key, "latest")
        return {
            "success": True,
            "message": f"Preloaded model '{model_registry_key}'.",
        }

    return router, session_router
