"""Durable training-job records and their Redis-backed state machine.

This module deliberately has no FastAPI, Celery, or model imports.  The route and
worker layers can share :class:`TrainingJobStore` without making either layer the
source of truth for lifecycle state.

Redis is used as the system of record.  Creation and every mutation that can
affect the lifecycle is a Lua script so the state check and write happen in one
Redis operation.  ``TrainingJobStore`` accepts a Redis client and a clock rather
than constructing either dependency, which keeps lifecycle tests deterministic
and avoids requiring a live Redis server.

Dataset listing reads the sorted-set index in bounded windows and lazily removes
expired members.  A background reaper is intentionally deferred: this core has
no scheduler, and removing an index member at terminal transition would discard
history before the terminal record's retention period elapsed.
"""

from __future__ import annotations

import json
import re
import traceback as traceback_module
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator


UTC = timezone.utc
Clock: TypeAlias = Callable[[], datetime]


class RedisClient(Protocol):
    """The synchronous Redis surface used by ``TrainingJobStore``."""

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        ...

    def get(self, name: str) -> Any:
        ...

    def zrevrange(self, name: str, start: int, end: int) -> Sequence[Any]:
        ...

    def zrem(self, name: str, *values: Any) -> Any:
        ...


class TrainingJobState(StrEnum):
    """States persisted for a training task.

    ``CANCEL_REQUESTED`` is intentionally non-terminal.  A running worker gets
    a chance to observe it at a batch boundary and then commits ``CANCELLED``.
    A queued task can be cancelled immediately because it has not started.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    REGISTERING = "REGISTERING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


# Alias kept short for callers that use ``JobState`` in route/task type hints.
JobState = TrainingJobState
TrainingJobStatus = TrainingJobState

TERMINAL_STATES = frozenset(
    {
        TrainingJobState.SUCCEEDED,
        TrainingJobState.FAILED,
        TrainingJobState.CANCELLED,
        TrainingJobState.TIMED_OUT,
    }
)

# This is both documentation and the source used to build the transition map
# passed into Redis Lua.  A transition to the same state is not a valid lifecycle
# transition; heartbeat/progress updates use the separate patch script.
ALLOWED_TRANSITIONS: Mapping[TrainingJobState, frozenset[TrainingJobState]] = {
    TrainingJobState.QUEUED: frozenset(
        {
            TrainingJobState.RUNNING,
            TrainingJobState.CANCELLED,
            TrainingJobState.TIMED_OUT,
            TrainingJobState.FAILED,
        }
    ),
    TrainingJobState.RUNNING: frozenset(
        {
            TrainingJobState.REGISTERING,
            TrainingJobState.CANCEL_REQUESTED,
            TrainingJobState.FAILED,
        }
    ),
    TrainingJobState.REGISTERING: frozenset(
        {
            TrainingJobState.SUCCEEDED,
            TrainingJobState.FAILED,
        }
    ),

    TrainingJobState.CANCEL_REQUESTED: frozenset({TrainingJobState.CANCELLED}),
    TrainingJobState.SUCCEEDED: frozenset(),
    TrainingJobState.FAILED: frozenset(),
    TrainingJobState.CANCELLED: frozenset(),
    TrainingJobState.TIMED_OUT: frozenset(),
}

DEFAULT_KEY_PREFIX = "iquana:training-jobs"
DEFAULT_TERMINAL_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_LIST_BATCH_SIZE = 128
MAX_PUBLIC_ERROR_MESSAGE_LENGTH = 240
MAX_TRACEBACK_LENGTH = 16_000


_ERROR_DEFAULT_MESSAGES = {
    "cancelled": "Training was cancelled.",
    "dispatch_failed": "Training could not be queued.",
    "missing_data": "Training data could not be loaded.",
    "nonfinite_loss": "Training produced an invalid loss.",
    "resource_exhausted": "Training ran out of available resources.",
    "start_deadline_exceeded": "Training did not start before its deadline.",
    "timeout": "Training infrastructure timed out.",
    "training_failed": "Training failed before completion.",
    "validation_error": "The training configuration is invalid.",
}
_ERROR_CODE_RE = re.compile(r"[^a-z0-9_.-]+")
_DATETIME_FIELDS = (
    "created_at",
    "queued_at",
    "start_deadline",
    "started_at",
    "heartbeat_at",
    "finished_at",
    "cancellation_requested_at",
)


def _normalise_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime, accepting naive test clocks as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    # Fixed-width UTC strings are lexicographically sortable in Lua, which lets
    # the timeout script compare a persisted deadline with the injected clock.
    return _normalise_datetime(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _clock_now(clock: Clock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("Training-job clock must return datetime")
    return _normalise_datetime(value)


def _sanitise_error_code(code: Any, *, fallback: str = "training_failed") -> str:
    value = str(code or "").strip().lower()
    value = _ERROR_CODE_RE.sub("_", value).strip("_.-")
    return (value or fallback)[:80]


def _sanitise_public_message(message: Any, *, fallback: str) -> str:
    # Exception text can contain paths, credentials, request data, or a full
    # traceback.  Collapse control/whitespace and cap the user-visible value.
    value = " ".join(str(message or "").split())
    if not value:
        value = fallback
    return value[:MAX_PUBLIC_ERROR_MESSAGE_LENGTH]


def _default_error_code(error: BaseException) -> str:
    if isinstance(error, (TimeoutError,)):
        return "timeout"
    if isinstance(error, MemoryError):
        return "resource_exhausted"
    if isinstance(error, (ValueError, TypeError)):
        return "validation_error"
    return "training_failed"


class TrainingJobError(BaseModel):
    """Safe error data intended for API/UI serialization.

    A traceback is deliberately not part of this model.  The job record has a
    separate ``error_traceback`` field for server-side diagnostics, and
    ``TrainingJob.to_public_dict`` excludes it.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str

    @field_validator("code", mode="before")
    @classmethod
    def _validate_code(cls, value: Any) -> str:
        return _sanitise_error_code(value)

    @field_validator("message", mode="before")
    @classmethod
    def _validate_message(cls, value: Any) -> str:
        return _sanitise_public_message(value, fallback=_ERROR_DEFAULT_MESSAGES["training_failed"])


ErrorInput: TypeAlias = BaseException | TrainingJobError | Mapping[str, Any] | str


def sanitise_error(
    error: ErrorInput,
    *,
    code: str | None = None,
    message: str | None = None,
) -> TrainingJobError:
    """Convert an arbitrary failure into safe, typed user-facing data.

    Exception messages are never used by default.  Callers may provide an
    explicitly chosen message when it is known to be safe; even then it is
    whitespace-normalised and length-limited.
    """

    if isinstance(error, TrainingJobError):
        return error

    if isinstance(error, BaseException):
        resolved_code = _sanitise_error_code(code or _default_error_code(error))
        fallback = _ERROR_DEFAULT_MESSAGES.get(
            resolved_code, _ERROR_DEFAULT_MESSAGES["training_failed"]
        )
        return TrainingJobError(
            code=resolved_code,
            message=_sanitise_public_message(message, fallback=fallback),
        )

    if isinstance(error, Mapping):
        resolved_code = _sanitise_error_code(code or error.get("code"))
        fallback = _ERROR_DEFAULT_MESSAGES.get(
            resolved_code, _ERROR_DEFAULT_MESSAGES["training_failed"]
        )
        # Mapping values often come directly from a worker/library response.
        # Their ``message`` is opaque internal data, not an approved public
        # message.  Only the explicit ``message=`` argument is trusted as a
        # caller-selected user-facing override.
        return TrainingJobError(
            code=resolved_code,
            message=_sanitise_public_message(
                message,
                fallback=fallback,
            ),
        )

    # A bare string is treated as opaque internal detail.  Callers that have a
    # deliberately safe user-facing string must pass it through ``message``;
    # otherwise an exception-like string could bypass the safe fallback.
    resolved_code = _sanitise_error_code(code)
    fallback = _ERROR_DEFAULT_MESSAGES.get(
        resolved_code, _ERROR_DEFAULT_MESSAGES["training_failed"]
    )
    return TrainingJobError(
        code=resolved_code,
        message=_sanitise_public_message(message, fallback=fallback),
    )


# US spelling is convenient for callers outside this repository.
sanitize_error = sanitise_error


def serialize_error(
    error: ErrorInput,
    *,
    code: str | None = None,
    message: str | None = None,
) -> dict[str, str]:
    """Return only the safe, JSON-serializable portion of an error."""

    return sanitise_error(error, code=code, message=message).model_dump(mode="json")


class TrainingJob(BaseModel):
    """Typed durable snapshot for one Celery training task."""

    model_config = ConfigDict(extra="forbid")

    # ``task_id`` is the Celery UUID and remains immutable for the record's
    # lifetime.  Keeping it inside the value as well as in the Redis key makes
    # task/run reconciliation explicit and protects against index corruption.
    task_id: str
    dataset_id: int | str
    user_id: int | str
    model_id: int | str | None = None
    model_registry_key: str | None = None
    run_name: str | None = None

    state: TrainingJobState = TrainingJobState.QUEUED
    attempt: int = 0

    created_at: datetime | None = None
    queued_at: datetime | None = None
    start_deadline: datetime | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None

    mlflow_run_id: str | None = None
    epoch: int | None = None
    total_epochs: int | None = None
    loss: float | None = None
    progress: float | None = None

    source_model_registry_key: str | None = None
    source_model_version: str | None = None
    source_model_uri: str | None = None
    output_model_registry_key: str | None = None
    output_model_version: str | None = None
    output_model_alias: str | None = None
    output_model_uri: str | None = None


    cancellation_requested_at: datetime | None = None
    error: TrainingJobError | None = None
    # This is retained for operators in Redis but excluded from public output.
    error_traceback: str | None = Field(default=None, exclude=True)

    @field_validator("task_id", mode="before")
    @classmethod
    def _validate_task_id(cls, value: Any) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("task_id must not be empty")
        return value

    @field_validator("attempt", "epoch", "total_epochs", mode="before")
    @classmethod
    def _validate_nonnegative_int(cls, value: Any) -> Any:
        if value is None:
            return value
        value = int(value)
        if value < 0:
            raise ValueError("training-job counters must be non-negative")
        return value

    @field_validator("progress")
    @classmethod
    def _validate_progress(cls, value: float | None) -> float | None:
        if value is not None and (not isfinite(value) or not 0 <= value <= 1):
            raise ValueError("progress must be a finite fraction between 0 and 1")
        return value

    @field_validator("loss")
    @classmethod
    def _validate_loss(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("loss must be finite")
        return value

    @field_validator(*_DATETIME_FIELDS)
    @classmethod
    def _validate_datetime(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _normalise_datetime(value)

    @field_validator("error_traceback", mode="before")
    @classmethod
    def _limit_traceback(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)[-MAX_TRACEBACK_LENGTH:]

    @property
    def celery_task_id(self) -> str:
        """Explicit alias used by worker integrations."""

        return self.task_id

    @property
    def model_key(self) -> int | str | None:
        """Return the registry key when no separate model id was supplied."""

        return self.model_id if self.model_id is not None else self.model_registry_key

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize without the server-side traceback."""

        return self.model_dump(mode="json", exclude={"error_traceback"})

    # ``to_dict`` is useful for route response adapters and keeps the public
    # serialization contract discoverable without coupling this module to FastAPI.
    to_dict = to_public_dict


TrainingJobRecord = TrainingJob


class TrainingJobUpdate(BaseModel):
    """Typed mutable fields accepted by heartbeat/progress updates."""

    model_config = ConfigDict(extra="forbid")

    started_at: datetime | None = None
    start_deadline: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    mlflow_run_id: str | None = None
    epoch: int | None = None
    total_epochs: int | None = None
    loss: float | None = None
    progress: float | None = None
    cancellation_requested_at: datetime | None = None
    error: TrainingJobError | None = None
    error_traceback: str | None = None
    run_name: str | None = None
    attempt: int | None = None
    output_model_registry_key: str | None = None
    output_model_version: str | None = None
    output_model_alias: str | None = None
    output_model_uri: str | None = None


    @field_validator(
        "started_at",
        "start_deadline",
        "heartbeat_at",
        "finished_at",
        "cancellation_requested_at",
    )
    @classmethod
    def _normalise_update_datetime(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _normalise_datetime(value)

    @field_validator("epoch", "total_epochs", "attempt", mode="before")
    @classmethod
    def _validate_update_counter(cls, value: Any) -> Any:
        if value is None:
            return value
        value = int(value)
        if value < 0:
            raise ValueError("training-job counters must be non-negative")
        return value

    @field_validator("progress")
    @classmethod
    def _validate_update_progress(cls, value: float | None) -> float | None:
        if value is not None and (not isfinite(value) or not 0 <= value <= 1):
            raise ValueError("progress must be a finite fraction between 0 and 1")
        return value

    @field_validator("loss")
    @classmethod
    def _validate_update_loss(cls, value: float | None) -> float | None:
        if value is not None and not isfinite(value):
            raise ValueError("loss must be finite")
        return value

    @field_validator("error_traceback", mode="before")
    @classmethod
    def _limit_update_traceback(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)[-MAX_TRACEBACK_LENGTH:]


class TrainingJobStoreError(RuntimeError):
    """Base error for durable training-job store failures."""


class TrainingJobNotFound(TrainingJobStoreError):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Training job '{task_id}' was not found")


class TrainingJobAlreadyExists(TrainingJobStoreError):
    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Training job '{task_id}' already exists")


class InvalidTrainingJobTransition(TrainingJobStoreError):
    def __init__(
        self,
        task_id: str,
        current_state: TrainingJobState | None,
        target_state: TrainingJobState,
    ):
        self.task_id = task_id
        self.current_state = current_state
        self.target_state = target_state
        current = current_state.value if current_state is not None else "missing"
        super().__init__(
            f"Training job '{task_id}' cannot transition from {current} to {target_state.value}"
        )


class TrainingJobDeadlineExceeded(InvalidTrainingJobTransition):
    """Raised when a worker tries to claim a queued job after its deadline."""

    def __init__(self, task_id: str, job: TrainingJob):
        self.job = job
        super().__init__(task_id, job.state, TrainingJobState.RUNNING)


class InvalidTrainingJobUpdate(TrainingJobStoreError):
    def __init__(self, task_id: str, current_state: TrainingJobState | None):
        self.task_id = task_id
        self.current_state = current_state
        current = current_state.value if current_state is not None else "missing"
        super().__init__(f"Training job '{task_id}' cannot be updated from {current}")


_TRANSITIONS_FOR_LUA = {
    state.value: sorted(target.value for target in targets)
    for state, targets in ALLOWED_TRANSITIONS.items()
}
_TRANSITIONS_JSON = json.dumps(_TRANSITIONS_FOR_LUA, separators=(",", ":"), sort_keys=True)
_TERMINAL_FOR_LUA = {state.value: True for state in TERMINAL_STATES}
_TERMINAL_JSON = json.dumps(_TERMINAL_FOR_LUA, separators=(",", ":"), sort_keys=True)


# The scripts return ``{status, payload}``.  Payload is the current/updated JSON
# record when available, which lets callers report a useful conflict without a
# second non-atomic read.
_CREATE_SCRIPT = r"""
-- iquana.training_jobs:create-v1
-- Validate both key types before the first write.  The pcall/compensation
-- path also prevents an unexpected write error from leaving an orphan record.
local job_type = redis.call("TYPE", KEYS[1]).ok
if job_type ~= "none" and job_type ~= "string" then
    return {-2, ""}
end
local existing = redis.call("GET", KEYS[1])
if existing then
    return {0, existing}
end
local index_type = redis.call("TYPE", KEYS[2]).ok
if index_type ~= "none" and index_type ~= "zset" then
    return {-3, ""}
end
local set_ok = pcall(redis.call, "SET", KEYS[1], ARGV[1])
if not set_ok then
    return {-4, ""}
end
local zadd_ok = pcall(redis.call, "ZADD", KEYS[2], ARGV[2], ARGV[3])
if not zadd_ok then
    local cleanup_ok = pcall(redis.call, "DEL", KEYS[1])
    if cleanup_ok then
        return {-4, ""}
    end
    return {-5, ""}
end
return {1, ARGV[1]}
"""

_TRANSITION_SCRIPT = r"""
-- iquana.training_jobs:transition-v1
local function is_json_null(value)
    return value == nil or value == cjson.null
end
local function apply_updates(record, updates)
    for key, value in pairs(updates) do
        -- Redis Lua's cjson.null is truthy, so turn JSON null into an absent
        -- optional field before later lifecycle logic reads the record.
        if value == cjson.null then
            record[key] = nil
        else
            record[key] = value
        end
    end
end
local payload = redis.call("GET", KEYS[1])
if not payload then
    return {0, ""}
end
local record = cjson.decode(payload)
if ARGV[1] ~= "" and record.state ~= ARGV[1] then
    return {-1, payload}
end
if ARGV[2] == "RUNNING"
    and record.state == "QUEUED"
    and not is_json_null(record.start_deadline)
    and record.start_deadline <= ARGV[3]
then
    record.state = "TIMED_OUT"
    record.finished_at = ARGV[3]
    record.heartbeat_at = ARGV[3]
    record.error = {
        code = "start_deadline_exceeded",
        message = "Training did not start before its deadline."
    }
    local encoded_timeout = cjson.encode(record)
    redis.call("SET", KEYS[1], encoded_timeout)
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[6]))
    return {-3, encoded_timeout}
end
local transitions = cjson.decode(ARGV[5])
local candidates = transitions[record.state]
local permitted = false
if candidates then
    for _, candidate in ipairs(candidates) do
        if candidate == ARGV[2] then
            permitted = true
            break
        end
    end
end
if not permitted then
    return {-2, payload}
end
local updates = cjson.decode(ARGV[4])
apply_updates(record, updates)
if ARGV[2] == "RUNNING" then
    record.attempt = (record.attempt or 0) + 1
end
record.state = ARGV[2]
local encoded = cjson.encode(record)
redis.call("SET", KEYS[1], encoded)
if ARGV[7] == "1" then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[6]))
end
return {1, encoded}
"""

_TIMEOUT_SCRIPT = r"""
-- iquana.training_jobs:timeout-v1
local function is_json_null(value)
    return value == nil or value == cjson.null
end
local function apply_updates(record, updates)
    for key, value in pairs(updates) do
        if value == cjson.null then
            record[key] = nil
        else
            record[key] = value
        end
    end
end
local payload = redis.call("GET", KEYS[1])
if not payload then
    return {0, ""}
end
local record = cjson.decode(payload)
if record.state ~= "QUEUED" then
    return {2, payload}
end
if is_json_null(record.start_deadline) or record.start_deadline > ARGV[1] then
    return {3, payload}
end
local updates = cjson.decode(ARGV[2])
apply_updates(record, updates)
record.state = "TIMED_OUT"
local encoded = cjson.encode(record)
redis.call("SET", KEYS[1], encoded)
redis.call("EXPIRE", KEYS[1], tonumber(ARGV[3]))
return {1, encoded}
"""

_CANCEL_SCRIPT = r"""
-- iquana.training_jobs:cancel-v1
local payload = redis.call("GET", KEYS[1])
if not payload then
    return {0, ""}
end
local record = cjson.decode(payload)
local state = record.state
if state == "SUCCEEDED" or state == "FAILED" or state == "CANCELLED" or state == "TIMED_OUT" then
    return {2, payload}
end
if state == "CANCEL_REQUESTED" then
    return {2, payload}
end
if state ~= "QUEUED" and state ~= "RUNNING" then
    return {-2, payload}
end
record.cancellation_requested_at = ARGV[1]
local terminal = state == "QUEUED"
if terminal then
    record.state = "CANCELLED"
    record.finished_at = ARGV[1]
else
    record.state = "CANCEL_REQUESTED"
end
record.heartbeat_at = ARGV[1]
local encoded = cjson.encode(record)
redis.call("SET", KEYS[1], encoded)
if terminal then
    redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
end
return {1, encoded}
"""

_PATCH_SCRIPT = r"""
-- iquana.training_jobs:patch-v1
local function apply_updates(record, updates)
    for key, value in pairs(updates) do
        if value == cjson.null then
            record[key] = nil
        else
            record[key] = value
        end
    end
end
local payload = redis.call("GET", KEYS[1])
if not payload then
    return {0, ""}
end
local record = cjson.decode(payload)
if ARGV[1] ~= "" and record.state ~= ARGV[1] then
    return {-1, payload}
end
local terminals = cjson.decode(ARGV[4])
if terminals[record.state] then
    return {-2, payload}
end
local updates = cjson.decode(ARGV[3])
apply_updates(record, updates)
local encoded = cjson.encode(record)
redis.call("SET", KEYS[1], encoded)
return {1, encoded}
"""


def _decode_scalar(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8")
    return str(value)


def _script_result(reply: Any) -> tuple[int, Any]:
    if not isinstance(reply, (list, tuple)) or len(reply) < 2:
        raise TrainingJobStoreError("Redis training-job script returned an invalid response")
    try:
        status = int(reply[0])
    except (TypeError, ValueError) as exc:
        raise TrainingJobStoreError("Redis training-job script returned an invalid status") from exc
    return status, reply[1]


def _decode_job(payload: Any) -> TrainingJob:
    if payload is None or payload == b"" or payload == "":
        raise TrainingJobStoreError("Redis returned an empty training-job record")
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8")
    if isinstance(payload, str):
        try:
            return TrainingJob.model_validate_json(payload)
        except ValueError as exc:
            raise TrainingJobStoreError("Redis contains an invalid training-job record") from exc
    if isinstance(payload, Mapping):
        try:
            return TrainingJob.model_validate(payload)
        except ValueError as exc:
            raise TrainingJobStoreError("Redis contains an invalid training-job record") from exc
    raise TrainingJobStoreError("Redis returned an unsupported training-job record")


def _normalise_update_payload(updates: Mapping[str, Any]) -> dict[str, Any]:
    update = TrainingJobUpdate.model_validate(updates)
    payload = update.model_dump(mode="json", exclude_unset=True)
    for field in _DATETIME_FIELDS:
        if field in payload:
            payload[field] = _format_datetime(getattr(update, field))
    return payload


def _traceback_for(error: BaseException) -> str:
    return "".join(
        traceback_module.format_exception(type(error), error, error.__traceback__)
    )[-MAX_TRACEBACK_LENGTH:]


def _coerce_error(
    error: ErrorInput | None,
    *,
    code: str | None = None,
    message: str | None = None,
    server_traceback: str | None = None,
) -> tuple[TrainingJobError, str | None]:
    if error is None:
        safe = sanitise_error("", code=code, message=message)
        return safe, server_traceback
    safe = sanitise_error(error, code=code, message=message)
    if isinstance(error, BaseException):
        return safe, server_traceback or _traceback_for(error)
    return safe, server_traceback


class TrainingJobStore:
    """Atomic Redis repository for durable training-job records."""

    def __init__(
        self,
        redis_client: RedisClient,
        *,
        clock: Clock | Any | None = None,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        terminal_ttl_seconds: int = DEFAULT_TERMINAL_TTL_SECONDS,
        list_batch_size: int = DEFAULT_LIST_BATCH_SIZE,
    ) -> None:
        normalised_prefix = key_prefix.strip().rstrip(":")
        if not normalised_prefix:
            raise ValueError("key_prefix must not be empty")
        try:
            normalised_ttl = int(terminal_ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("terminal_ttl_seconds must be a positive integer") from exc
        if isinstance(terminal_ttl_seconds, float) and terminal_ttl_seconds != normalised_ttl:
            raise ValueError("terminal_ttl_seconds must be a positive integer")
        if normalised_ttl <= 0:
            raise ValueError("terminal_ttl_seconds must be positive")
        try:
            normalised_batch_size = int(list_batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("list_batch_size must be a positive integer") from exc
        if isinstance(list_batch_size, float) and list_batch_size != normalised_batch_size:
            raise ValueError("list_batch_size must be a positive integer")
        if normalised_batch_size <= 0:
            raise ValueError("list_batch_size must be positive")
        self.redis = redis_client
        if clock is None:
            self._clock: Clock = lambda: datetime.now(UTC)
        elif callable(clock):
            self._clock = clock
        elif callable(getattr(clock, "now", None)):
            self._clock = clock.now
        else:
            raise TypeError("clock must be callable or provide now()")
        self.key_prefix = normalised_prefix
        self.terminal_ttl_seconds = normalised_ttl
        self.list_batch_size = normalised_batch_size

    def job_key(self, task_id: str) -> str:
        return f"{self.key_prefix}:job:{task_id}"

    key_for_task = job_key

    def dataset_index_key(self, dataset_id: int | str) -> str:
        return f"{self.key_prefix}:dataset:{dataset_id}"

    index_key_for_dataset = dataset_index_key

    def _now(self, at: datetime | None = None) -> datetime:
        return _normalise_datetime(at) if at is not None else _clock_now(self._clock)

    @staticmethod
    def _storage_payload(job: TrainingJob) -> str:
        payload = job.model_dump(mode="json")
        # ``error_traceback`` is excluded by the model itself so a route cannot
        # accidentally expose it through a normal ``model_dump``.  The Redis
        # repository is the one trusted internal serialization boundary that
        # retains it for operators.
        if job.error_traceback is not None:
            payload["error_traceback"] = job.error_traceback
        for field in _DATETIME_FIELDS:
            payload[field] = _format_datetime(getattr(job, field))
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def _eval(self, script: str, keys: Sequence[str], args: Sequence[Any]) -> Any:
        try:
            return self.redis.eval(script, len(keys), *keys, *args)
        except Exception as exc:
            raise TrainingJobStoreError("Redis training-job operation failed") from exc

    def create(self, job: TrainingJob) -> TrainingJob:
        """Atomically persist a queued record and add it to its dataset index."""

        if job.state is not TrainingJobState.QUEUED:
            raise ValueError("new training jobs must start in QUEUED")
        now = self._now()
        created_at = job.created_at or now
        queued_at = job.queued_at or created_at
        normalised = job.model_copy(
            update={
                "created_at": _normalise_datetime(created_at),
                "queued_at": _normalise_datetime(queued_at),
                "model_id": job.model_id if job.model_id is not None else job.model_registry_key,
            }
        )
        if normalised.finished_at is not None or normalised.error is not None:
            raise ValueError("queued training jobs cannot have terminal fields")
        reply = self._eval(
            _CREATE_SCRIPT,
            (self.job_key(normalised.task_id), self.dataset_index_key(normalised.dataset_id)),
            (
                self._storage_payload(normalised),
                str(normalised.created_at.timestamp()),
                normalised.task_id,
            ),
        )
        status, payload = _script_result(reply)
        if status == 0:
            raise TrainingJobAlreadyExists(normalised.task_id)
        if status in (-2, -3):
            raise TrainingJobStoreError(
                "Redis training-job storage has an incompatible key type"
            )
        if status in (-4, -5):
            raise TrainingJobStoreError("Redis training-job creation failed atomically")
        if status != 1:
            raise TrainingJobStoreError("Redis refused to create training-job record")
        return _decode_job(payload)

    def enqueue(
        self,
        *,
        task_id: str,
        dataset_id: int | str,
        user_id: int | str,
        model_id: int | str | None = None,
        model_registry_key: str | None = None,
        run_name: str | None = None,
        start_deadline: datetime | None = None,
        created_at: datetime | None = None,
        source_model_registry_key: str | None = None,
        source_model_version: str | None = None,
        source_model_uri: str | None = None,
        output_model_registry_key: str | None = None,
        output_model_version: str | None = None,
        output_model_alias: str | None = None,
        output_model_uri: str | None = None,
    ) -> TrainingJob:
        """Build and atomically persist a queued job before dispatch."""

        return self.create(
            TrainingJob(
                task_id=task_id,
                dataset_id=dataset_id,
                user_id=user_id,
                model_id=model_id if model_id is not None else model_registry_key,
                model_registry_key=model_registry_key,
                run_name=run_name,
                start_deadline=start_deadline,
                created_at=created_at,
                source_model_registry_key=source_model_registry_key,
                source_model_version=source_model_version,
                source_model_uri=source_model_uri,
                output_model_registry_key=output_model_registry_key,
                output_model_version=output_model_version,
                output_model_alias=output_model_alias,
                output_model_uri=output_model_uri,
            )
        )


    create_queued = enqueue

    def get(self, task_id: str) -> TrainingJob | None:
        try:
            payload = self.redis.get(self.job_key(task_id))
        except Exception as exc:
            raise TrainingJobStoreError("Redis training-job read failed") from exc
        if payload is None:
            return None
        return _decode_job(payload)

    def require(self, task_id: str) -> TrainingJob:
        job = self.get(task_id)
        if job is None:
            raise TrainingJobNotFound(task_id)
        return job

    get_required = require

    @staticmethod
    def _state(value: TrainingJobState | str) -> TrainingJobState:
        try:
            return value if isinstance(value, TrainingJobState) else TrainingJobState(value)
        except ValueError as exc:
            raise ValueError(f"unknown training-job state: {value}") from exc

    @staticmethod
    def _update_payload(updates: Mapping[str, Any]) -> str:
        return json.dumps(_normalise_update_payload(updates), separators=(",", ":"), sort_keys=True)

    def _raise_transition_error(
        self,
        task_id: str,
        target: TrainingJobState,
        status: int,
        payload: Any,
    ) -> None:
        if status == 0:
            raise TrainingJobNotFound(task_id)
        current: TrainingJob | None = None
        if payload not in (None, "", b""):
            current = _decode_job(payload)
        if status in (-1, -2):
            raise InvalidTrainingJobTransition(
                task_id,
                current.state if current is not None else None,
                target,
            )
        if status == -3 and current is not None:
            raise TrainingJobDeadlineExceeded(task_id, current)
        raise TrainingJobStoreError("Redis rejected training-job transition")

    def transition(
        self,
        task_id: str,
        target_state: TrainingJobState | str,
        *,
        expected_state: TrainingJobState | str | None = None,
        at: datetime | None = None,
        error: ErrorInput | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        server_traceback: str | None = None,
        **field_updates: Any,
    ) -> TrainingJob:
        """Atomically apply one allowed lifecycle transition.

        ``expected_state`` is optional for callers that only need the graph
        enforced.  Worker entry points should provide it to make stale task
        deliveries fail explicitly.
        """

        target = self._state(target_state)
        expected = "" if expected_state is None else self._state(expected_state).value
        now = self._now(at)
        updates = dict(field_updates)

        if target is TrainingJobState.RUNNING:
            updates.setdefault("started_at", now)
            updates.setdefault("heartbeat_at", now)
        elif target in {
            TrainingJobState.REGISTERING,
            TrainingJobState.CANCEL_REQUESTED,
        }:
            updates.setdefault("heartbeat_at", now)
        if target is TrainingJobState.CANCEL_REQUESTED:
            updates.setdefault("cancellation_requested_at", now)
        if target in TERMINAL_STATES:
            updates.setdefault("finished_at", now)
            updates.setdefault("heartbeat_at", now)

        if target is TrainingJobState.FAILED:
            safe_error, safe_traceback = _coerce_error(
                error,
                # Passing None is significant: sanitise_error classifies the
                # exception (timeout, validation, OOM, or unknown) instead of
                # collapsing every failure to training_failed.
                code=error_code,
                message=error_message,
                server_traceback=server_traceback,
            )
            updates.setdefault("error", safe_error)
            if safe_traceback is not None:
                updates.setdefault("error_traceback", safe_traceback)
        elif error is not None:
            safe_error, safe_traceback = _coerce_error(
                error,
                code=error_code,
                message=error_message,
                server_traceback=server_traceback,
            )
            updates.setdefault("error", safe_error)
            if safe_traceback is not None:
                updates.setdefault("error_traceback", safe_traceback)
        elif server_traceback is not None:
            updates.setdefault("error_traceback", server_traceback)

        reply = self._eval(
            _TRANSITION_SCRIPT,
            (self.job_key(task_id),),
            (
                expected,
                target.value,
                _format_datetime(now),
                self._update_payload(updates),
                _TRANSITIONS_JSON,
                str(self.terminal_ttl_seconds),
                "1" if target in TERMINAL_STATES else "0",
            ),
        )
        status, payload = _script_result(reply)
        if status != 1:
            self._raise_transition_error(task_id, target, status, payload)
        return _decode_job(payload)

    def mark_running(self, task_id: str, *, at: datetime | None = None) -> TrainingJob:
        return self.transition(
            task_id,
            TrainingJobState.RUNNING,
            expected_state=TrainingJobState.QUEUED,
            at=at,
        )

    def mark_registering(self, task_id: str, *, at: datetime | None = None) -> TrainingJob:
        return self.transition(
            task_id,
            TrainingJobState.REGISTERING,
            expected_state=TrainingJobState.RUNNING,
            at=at,
        )

    def mark_succeeded(
        self,
        task_id: str,
        *,
        mlflow_run_id: str | None = None,
        at: datetime | None = None,
    ) -> TrainingJob:
        updates = {} if mlflow_run_id is None else {"mlflow_run_id": mlflow_run_id}
        return self.transition(
            task_id,
            TrainingJobState.SUCCEEDED,
            expected_state=TrainingJobState.REGISTERING,
            at=at,
            **updates,
        )

    def mark_failed(
        self,
        task_id: str,
        error: ErrorInput | None = None,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
        server_traceback: str | None = None,
        expected_state: TrainingJobState | str | None = None,
        at: datetime | None = None,
    ) -> TrainingJob:
        return self.transition(
            task_id,
            TrainingJobState.FAILED,
            expected_state=expected_state,
            at=at,
            error=error,
            error_code=error_code,
            error_message=error_message,
            server_traceback=server_traceback,
        )

    def request_cancellation(self, task_id: str, *, at: datetime | None = None) -> TrainingJob:
        """Durably request cancellation, with a terminal winner under races.

        Queued work is cancelled immediately.  Running/registering work moves
        to ``CANCEL_REQUESTED`` and must later commit ``CANCELLED`` cooperatively.
        A request after a terminal transition returns that terminal snapshot and
        does not overwrite it.
        """

        now = self._now(at)
        reply = self._eval(
            _CANCEL_SCRIPT,
            (self.job_key(task_id),),
            (_format_datetime(now), str(self.terminal_ttl_seconds)),
        )
        status, payload = _script_result(reply)
        if status == 0:
            raise TrainingJobNotFound(task_id)
        if status in (1, 2):
            return _decode_job(payload)
        current = _decode_job(payload) if payload not in (None, "", b"") else None
        raise InvalidTrainingJobTransition(
            task_id,
            current.state if current else None,
            TrainingJobState.CANCEL_REQUESTED,
        )

    request_cancel = request_cancellation
    cancel = request_cancellation

    def mark_cancelled(self, task_id: str, *, at: datetime | None = None) -> TrainingJob:
        return self.transition(
            task_id,
            TrainingJobState.CANCELLED,
            expected_state=TrainingJobState.CANCEL_REQUESTED,
            at=at,
        )

    def timeout_if_expired(self, task_id: str, *, at: datetime | None = None) -> TrainingJob:
        """Atomically time out an expired queued task.

        If another actor already started or terminally resolved the task, the
        current snapshot is returned unchanged.  This is what makes timeout vs
        worker-start a single-winner race.
        """

        now = self._now(at)
        timeout_error = TrainingJobError(
            code="start_deadline_exceeded",
            message=_ERROR_DEFAULT_MESSAGES["start_deadline_exceeded"],
        )
        updates = {
            "finished_at": now,
            "heartbeat_at": now,
            "error": timeout_error,
        }
        reply = self._eval(
            _TIMEOUT_SCRIPT,
            (self.job_key(task_id),),
            (
                _format_datetime(now),
                self._update_payload(updates),
                str(self.terminal_ttl_seconds),
            ),
        )
        status, payload = _script_result(reply)
        if status == 0:
            raise TrainingJobNotFound(task_id)
        if status in (1, 2, 3):
            return _decode_job(payload)
        raise TrainingJobStoreError("Redis rejected training-job timeout")

    timeout = timeout_if_expired
    mark_timed_out = timeout_if_expired

    def update_progress(
        self,
        task_id: str,
        *,
        epoch: int | None = None,
        total_epochs: int | None = None,
        attempt: int | None = None,
        loss: float | None = None,
        progress: float | None = None,
        mlflow_run_id: str | None = None,
        at: datetime | None = None,
        expected_state: TrainingJobState | str | None = None,
    ) -> TrainingJob:
        """Atomically update progress/heartbeat without changing lifecycle state."""

        now = self._now(at)
        updates: dict[str, Any] = {"heartbeat_at": now}
        for name, value in (
            ("epoch", epoch),
            ("total_epochs", total_epochs),
            ("attempt", attempt),
            ("loss", loss),
            ("progress", progress),
            ("mlflow_run_id", mlflow_run_id),
        ):
            if value is not None:
                updates[name] = value
        expected = "" if expected_state is None else self._state(expected_state).value
        reply = self._eval(
            _PATCH_SCRIPT,
            (self.job_key(task_id),),
            (
                expected,
                _format_datetime(now),
                self._update_payload(updates),
                _TERMINAL_JSON,
            ),
        )
        status, payload = _script_result(reply)
        if status == 0:
            raise TrainingJobNotFound(task_id)
        if status in (-1, -2):
            current = _decode_job(payload) if payload not in (None, "", b"") else None
            raise InvalidTrainingJobUpdate(task_id, current.state if current else None)
        if status != 1:
            raise TrainingJobStoreError("Redis rejected training-job progress update")
        return _decode_job(payload)

    def patch(
        self,
        task_id: str,
        updates: Mapping[str, Any],
        *,
        at: datetime | None = None,
        expected_state: TrainingJobState | str | None = None,
    ) -> TrainingJob:
        """Patch fields on an active job record without changing lifecycle state."""
        now = self._now(at)
        field_updates: dict[str, Any] = {"heartbeat_at": now}
        field_updates.update(updates)
        expected = "" if expected_state is None else self._state(expected_state).value
        reply = self._eval(
            _PATCH_SCRIPT,
            (self.job_key(task_id),),
            (
                expected,
                _format_datetime(now),
                self._update_payload(field_updates),
                _TERMINAL_JSON,
            ),
        )
        status, payload = _script_result(reply)
        if status == 0:
            raise TrainingJobNotFound(task_id)
        if status in (-1, -2):
            current = _decode_job(payload) if payload not in (None, "", b"") else None
            raise InvalidTrainingJobUpdate(task_id, current.state if current else None)
        return _decode_job(payload)

    heartbeat = update_progress

    def list_for_dataset(
        self,
        dataset_id: int | str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TrainingJob]:
        """List live records from the dataset's sorted-set index.

        Terminal job keys expire independently.  Missing members are pruned
        opportunistically so the index cannot grow forever after TTL expiry.
        """

        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be non-negative")
        if limit == 0:
            return []
        index_key = self.dataset_index_key(dataset_id)
        jobs: list[TrainingJob] = []
        expected_dataset = str(dataset_id)
        valid_seen = 0
        scan_start = 0
        while len(jobs) < limit:
            try:
                # Never materialise the complete sorted set.  Expired members
                # can still make a listing walk a long stale tail; bounded
                # windows keep memory and each Redis response proportional to
                # the requested page.  A scheduler-backed reaper remains a
                # deliberate follow-up for fully eliminating stale-tail scans.
                members = self.redis.zrevrange(
                    index_key,
                    scan_start,
                    scan_start + self.list_batch_size - 1,
                )
            except Exception as exc:
                raise TrainingJobStoreError("Redis training-job index read failed") from exc
            if not members:
                break
            for member in members:
                task_id = _decode_scalar(member)
                job = self.get(task_id)
                if job is None:
                    try:
                        self.redis.zrem(index_key, task_id)
                    except Exception as exc:
                        raise TrainingJobStoreError(
                            "Redis training-job index cleanup failed"
                        ) from exc
                    # Removing the current member shifts the range left, so
                    # keep the absolute start at the same position.
                    continue
                if str(job.dataset_id) != expected_dataset:
                    # Do not return an incorrectly indexed record; repair the
                    # stale index member while preserving the canonical job
                    # record (which may belong to another dataset's index).
                    try:
                        self.redis.zrem(index_key, task_id)
                    except Exception as exc:
                        raise TrainingJobStoreError(
                            "Redis training-job index cleanup failed"
                        ) from exc
                    continue
                scan_start += 1
                if valid_seen < offset:
                    valid_seen += 1
                    continue
                jobs.append(job)
                if len(jobs) >= limit:
                    break
        return jobs

    list_by_dataset = list_for_dataset
    list_jobs_for_dataset = list_for_dataset


__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_LIST_BATCH_SIZE",
    "DEFAULT_KEY_PREFIX",
    "DEFAULT_TERMINAL_TTL_SECONDS",
    "InvalidTrainingJobTransition",
    "InvalidTrainingJobUpdate",
    "JobState",
    "RedisClient",
    "TERMINAL_STATES",
    "TrainingJob",
    "TrainingJobRecord",
    "TrainingJobAlreadyExists",
    "TrainingJobDeadlineExceeded",
    "TrainingJobError",
    "TrainingJobNotFound",
    "TrainingJobState",
    "TrainingJobStatus",
    "TrainingJobStore",
    "TrainingJobStoreError",
    "TrainingJobUpdate",
    "sanitize_error",
    "sanitise_error",
    "serialize_error",
]
