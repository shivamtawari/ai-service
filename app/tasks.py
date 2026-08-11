import logging
import traceback
from typing import Any

import mlflow
from celery.exceptions import Reject
from iquana_toolbox.ai.base_classes import InstanceSegmentationModel
from iquana_toolbox.mlflow import MLFlowModelRegistry
from iquana_toolbox.schemas.training import InstanceSegmentationTrainingRequest

from app.state import TRAINING_JOB_STORE
from app.training_jobs import (
    JobState,
    TrainingJobStoreError,
    TrainingJobNotFound,
    InvalidTrainingJobTransition,
    TrainingJobDeadlineExceeded,
)
from celery_app import app
from paths import MLFLOW_URL

logger = logging.getLogger(__name__)

# Training runs are grouped under this MLflow experiment.
TRAINING_EXPERIMENT = "instance-segmentation-training"


@app.task(bind=True, acks_late=True, reject_on_worker_lost=True)
def train_and_register_model(self, request_dict: dict, model_run_name: str | None = None):
    """Generic training dispatcher."""
    task_id = self.request.id
    
    try:
        if self.request.retries == 0:
            job = TRAINING_JOB_STORE.mark_running(task_id)
        else:
            job = TRAINING_JOB_STORE.require(task_id)
    except TrainingJobNotFound:
        logger.warning(f"Task {task_id} not found in store; rejecting.")
        raise Reject("Task not found in durable store.")
    except TrainingJobDeadlineExceeded:
        logger.warning(f"Task {task_id} exceeded deadline before starting; rejecting.")
        return {"status": "timed_out"}
    except InvalidTrainingJobTransition as e:
        logger.warning(f"Task {task_id} cannot transition to RUNNING: {e}; rejecting.")
        raise Reject(f"Invalid transition: {e}")
    except TrainingJobStoreError as e:
        logger.error(f"Failed to read task {task_id} from store: {e}")
        if self.request.retries >= self.max_retries:
            try:
                TRAINING_JOB_STORE.mark_failed(
                    task_id,
                    error_code="infrastructure_error",
                    error_message="Failed to read task from durable store.",
                    server_traceback=__import__('traceback').format_exc()
                )
            except Exception:
                pass
            raise Reject(f"Terminal error claiming task: {e}")
        raise self.retry(countdown=10, max_retries=3, exc=e)
        
    try:
        registry = MLFlowModelRegistry(MLFLOW_URL)
        request = InstanceSegmentationTrainingRequest.model_validate(request_dict)
        
        source_version = job.source_model_version if (job and getattr(job, "source_model_version", None)) else "latest"
        pyfunc_model = registry.get_model_by_version(request.model_registry_key, source_version)
        model: InstanceSegmentationModel = pyfunc_model._model_impl.python_model

        mlflow.set_tracking_uri(MLFLOW_URL)
        mlflow.set_experiment(TRAINING_EXPERIMENT)

        with mlflow.start_run(run_name=f"train_{request.model_registry_key}_ds{request.dataset_id}") as active_run:
            mlflow.set_tag("celery_task_id", task_id)
            mlflow.set_tag("dataset_id", str(request.dataset_id))
            mlflow.set_tag("user_id", str(request.user_id))
            if model_run_name:
                mlflow.set_tag("run_name", model_run_name)
                
            def progress_callback(update: dict[str, int | float]) -> None:
                TRAINING_JOB_STORE.update_progress(
                    task_id, 
                    epoch=update.get("epoch"), 
                    loss=update.get("loss"),
                    mlflow_run_id=active_run.info.run_id,
                )

            def is_cancelled() -> bool:
                try:
                    current_job = TRAINING_JOB_STORE.require(task_id)
                    return current_job.state == JobState.CANCEL_REQUESTED
                except TrainingJobStoreError:
                    return False
                
            # Log initial run ID to the store
            TRAINING_JOB_STORE.update_progress(
                task_id,
                mlflow_run_id=active_run.info.run_id,
            )
            
            # Start the actual training
            from models.mask2former import TrainingCancelled
            
            model.train(
                request, 
                progress_callback=progress_callback, 
                is_cancelled=is_cancelled
            )

        # Transition to REGISTERING
        try:
            TRAINING_JOB_STORE.mark_registering(task_id)
        except InvalidTrainingJobTransition:
            job = TRAINING_JOB_STORE.require(task_id)
            if job.state == JobState.CANCEL_REQUESTED:
                TRAINING_JOB_STORE.mark_cancelled(task_id)
                return {"status": "cancelled"}
            raise
        
        job = TRAINING_JOB_STORE.require(task_id)
        output_key = job.output_model_registry_key or f"mask2former-ds{request.dataset_id}-{task_id}"

        # Construct trained model_info for publication without mutating shared base
        from iquana_toolbox.schemas.model_info import InstanceSegmentationModelInfo
        trained_info = InstanceSegmentationModelInfo(
            registry_key=output_key,
            name=model_run_name or f"Custom Mask2Former (DS {request.dataset_id})",
            description=f"Hierarchy-aware instance segmentation trained on dataset {request.dataset_id}",
            usage_tip=getattr(model.model_info, "usage_tip", None) or "Trained model",
            model_role="trained",
            base_model_registry_key=job.source_model_registry_key or request.model_registry_key,
            base_model_version=job.source_model_version,
            base_model_uri=job.source_model_uri,
            training_task_id=task_id,
            dataset_id=request.dataset_id,
            trained_by=request.user_id,
            label_ids=request.selected_label_ids,
            segmentation_mode="hierarchical" if request.enable_hierarchy else "flat",
            target_encoding="exclusive_hierarchy_v1" if request.enable_hierarchy else "standard",
            tags={
                "dataset_id": str(request.dataset_id),
                "user_id": str(request.user_id),
                "training_task_id": task_id,
            },
        )
        model.model_info = trained_info

        pub_result = registry.register_model(model, assign_alias="active")
        if not pub_result or not pub_result.version or not pub_result.model_uri:
            raise RuntimeError(f"Model registration for key '{output_key}' did not return a valid publication result.")

        output_version = pub_result.version
        output_alias = pub_result.alias or "active"
        output_uri = pub_result.model_uri

        TRAINING_JOB_STORE.patch(
            task_id,
            {
                "output_model_registry_key": output_key,
                "output_model_version": output_version,
                "output_model_alias": output_alias,
                "output_model_uri": output_uri,
            }
        )

        TRAINING_JOB_STORE.mark_succeeded(task_id)
        return {"status": "completed"}


        
    except Exception as e:
        # Preserve the original exception and traceback in both worker logs and
        # Celery's retry/final-failure result.
        logger.error("Training failed: %s", e, exc_info=True)
        from models.mask2former import TrainingCancelled
        
        if isinstance(e, TrainingCancelled):
            TRAINING_JOB_STORE.mark_cancelled(task_id)
            return {"status": "cancelled"}
            
        # Distinguish between transient and permanent errors. 
        # For simplicity, we assume validation/data errors are terminal.
        # Check if the error is a known terminal exception
        from redis.exceptions import ConnectionError, TimeoutError
        from requests.exceptions import RequestException
        
        is_transient = isinstance(e, (ConnectionError, TimeoutError, RequestException))
        
        if is_transient:
            if self.request.retries >= self.max_retries:
                tb = traceback.format_exc()
                try:
                    TRAINING_JOB_STORE.mark_failed(
                        task_id,
                        error_code="training_failed",
                        error_message="Training failed after maximum retries.",
                        server_traceback=tb
                    )
                except Exception:
                    logger.exception("Could not write terminal failure to Redis.")
                raise Reject(f"Terminal error: {e}")
                
            # We don't mark failed here; the retry will handle it, or it will fail eventually
            logger.warning(f"Transient error for {task_id}, retrying: {e}")
            raise self.retry(countdown=60, exc=e)
        else:
            # Terminal error
            tb = traceback.format_exc()
            try:
                TRAINING_JOB_STORE.mark_failed(
                    task_id,
                    error_code="training_failed",
                    error_message="Training failed due to a permanent error.",
                    server_traceback=tb
                )
            except Exception:
                logger.exception("Could not write terminal failure to Redis.")
                
            # Still raise Reject so Celery knows it's failed, but don't retry
            raise Reject(f"Terminal error: {e}")
