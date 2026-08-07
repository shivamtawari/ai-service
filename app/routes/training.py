"""Training surface (mounted under ``/instance-segmentation``).

Ported from the former instance-segmentation-service. Submits a Celery job and
returns its task id; the gateway polls progress via MLflow (the task tags its
run with ``celery_task_id``).
"""
import logging
import uuid
from typing import Optional, Any

from celery.result import AsyncResult
from fastapi import APIRouter, HTTPException, Query, status
from iquana_toolbox.schemas.training import InstanceSegmentationTrainingRequest

from app.state import TRAINING_JOB_STORE
from app.tasks import train_and_register_model
from app.training_jobs import TrainingJobNotFound, TrainingJobStoreError, InvalidTrainingJobTransition
from util.validate_model import validate_model

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/train")
async def start_training(
    request: InstanceSegmentationTrainingRequest,
    model_run_name: Optional[str] = None,
):
    """Start a training job asynchronously. Delegates to Celery workers.

    Args:
        request: Typed training configuration (labels, hyperparameters, etc.).
        model_run_name: Optional human-readable alias for this run stored as an
            MLflow tag.  Surfaced in the run-history UI as a display name.
    """
    validate_model(request)
    
    # 3. Generate the Celery UUID and persist QUEUED before apply_async
    task_id = str(uuid.uuid4())
    
    try:
        job = TRAINING_JOB_STORE.enqueue(
            task_id=task_id,
            dataset_id=request.dataset_id,
            user_id=request.user_id,
            model_registry_key=request.model_registry_key,
            run_name=model_run_name,
            start_deadline=__import__('datetime').datetime.now(__import__('datetime').timezone.utc) + __import__('datetime').timedelta(minutes=15)
        )
    except TrainingJobStoreError as e:
        logger.exception("Failed to persist QUEUED training job in Redis.")
        # 4. Return 503 and dispatch nothing when Redis persistence fails
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not persist training job in system of record.",
        ) from e

    try:
        train_and_register_model.apply_async(
            kwargs={
                "request_dict": request.model_dump(),
                "model_run_name": model_run_name,
            },
            task_id=task_id,
        )
    except Exception as e:
        logger.exception("Failed to dispatch to Celery after enqueueing.")
        # Mark a dispatch failure terminal without leaving an ambiguous queued record
        try:
            TRAINING_JOB_STORE.mark_failed(
                task_id,
                error_code="dispatch_failed",
                error_message="Failed to dispatch job to worker queue.",
                server_traceback=None,
            )
        except Exception:
            logger.exception("Failed to mark dispatch failure as terminal.")
        
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not dispatch job to worker queue.",
        ) from e

    return {"task_id": task_id}


@router.delete("/train/{task_id}")
async def cancel_training(task_id: str):
    """Cancel a training job cooperatively."""
    try:
        job = TRAINING_JOB_STORE.request_cancellation(task_id)
    except TrainingJobNotFound:
        raise HTTPException(status_code=404, detail="Training job not found")
    except InvalidTrainingJobTransition as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid transition: {e}"
        ) from e
    except TrainingJobStoreError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not reach durable store: {e}"
        ) from e

    # Revoke with terminate=False for cooperative cancellation
    task = AsyncResult(task_id)
    task.revoke(terminate=False)
    
    return {"message": "Training cancellation requested", "task_id": task_id}


@router.get("/train/{task_id}")
async def get_training_task_state(task_id: str) -> dict[str, Any]:
    """Return the durable state of a training job."""
    # 5. Reconcile stale queued records to TIMED_OUT
    try:
        job = TRAINING_JOB_STORE.timeout_if_expired(task_id)
    except TrainingJobNotFound:
        raise HTTPException(status_code=404, detail="Training job not found")
        
    return job.to_public_dict()


@router.get("/train/dataset/{dataset_id}")
async def list_training_jobs(
    dataset_id: int,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """List training jobs for a dataset."""
    jobs = TRAINING_JOB_STORE.list_for_dataset(
        dataset_id=dataset_id,
        limit=limit,
        offset=offset,
    )
    reconciled_jobs = []
    from app.training_jobs import JobState
    for job in jobs:
        if job.state == JobState.QUEUED:
            job = TRAINING_JOB_STORE.timeout_if_expired(job.task_id)
        reconciled_jobs.append(job)
    return [job.to_public_dict() for job in reconciled_jobs]
