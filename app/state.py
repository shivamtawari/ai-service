from iquana_toolbox.mlflow import MLFlowModelRegistry

from paths import MLFLOW_URL

# One shared, MLflow-backed registry for the whole service. It is the single
# catalog every task surface filters by tag, and the cache all models load into.
MODEL_REGISTRY = MLFlowModelRegistry(MLFLOW_URL)
import datetime
import redis

from paths import REDIS_URL
from app.training_jobs import TrainingJobStore

_redis_client = redis.from_url(f"{REDIS_URL}/2", decode_responses=True)
TRAINING_JOB_STORE = TrainingJobStore(
    redis_client=_redis_client,
    clock=lambda: datetime.datetime.now(datetime.timezone.utc),
)
