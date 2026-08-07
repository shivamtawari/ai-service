"""Focused tests for the Redis-backed durable training-job state machine.

The fake below implements the small Redis surface used by ``TrainingJobStore``
and executes the same atomic operation semantics under a lock.  No Redis server
or network is needed; the production module still uses Lua scripts for the real
client, so compare-and-transition behavior remains atomic across processes.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import training_jobs as jobs


UTC = timezone.utc
START = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


class _CjsonNull:
    """Truth-y stand-in for Redis Lua's ``cjson.null`` sentinel."""

    def __bool__(self) -> bool:
        return True


CJSON_NULL = _CjsonNull()


def _as_cjson(value: Any) -> Any:
    if value is None:
        return CJSON_NULL
    if isinstance(value, dict):
        return {key: _as_cjson(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_as_cjson(child) for child in value]
    return value


def _from_cjson(value: Any) -> Any:
    if value is CJSON_NULL:
        return None
    if isinstance(value, dict):
        return {key: _from_cjson(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_from_cjson(child) for child in value]
    return value


def _has_json_value(value: Any) -> bool:
    return value is not None and value is not CJSON_NULL


def _apply_updates(record: dict[str, Any], payload: str) -> None:
    for key, value in _as_cjson(json.loads(payload)).items():
        if value is CJSON_NULL:
            record.pop(key, None)
        else:
            record[key] = value


class FrozenClock:
    def __init__(self, value: datetime = START):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: int) -> None:
        self.value += timedelta(**delta)


class MemoryRedis:
    """Small locked fake for the Redis commands used by the job store."""

    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}
        self._expires: dict[str, float] = {}
        self._sorted_sets: dict[str, dict[str, float]] = {}
        self._types: dict[str, str] = {}
        self._lock = threading.RLock()
        self._offset = 0.0
        self.range_calls: list[tuple[str, int, int]] = []
        self.fail_next_zadd = False

    def _time(self) -> float:
        return time.monotonic() + self._offset

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._offset += seconds

    def _purge(self, name: str) -> None:
        expiry = self._expires.get(name)
        if expiry is not None and expiry <= self._time():
            self._values.pop(name, None)
            self._expires.pop(name, None)
            self._types.pop(name, None)

    def get(self, name: str) -> bytes | None:
        with self._lock:
            self._purge(name)
            if self._types.get(name) not in (None, "string"):
                raise TypeError(f"WRONGTYPE GET {name}")
            return self._values.get(name)

    def ttl(self, name: str) -> int:
        with self._lock:
            self._purge(name)
            if name not in self._values:
                return -2
            expiry = self._expires.get(name)
            if expiry is None:
                return -1
            return max(0, int(expiry - self._time()))

    def zrevrange(self, name: str, start: int, end: int) -> list[bytes]:
        with self._lock:
            self.range_calls.append((name, start, end))
            if self._types.get(name) not in (None, "zset"):
                raise TypeError(f"WRONGTYPE ZREVRANGE {name}")
            values = sorted(
                self._sorted_sets.get(name, {}).items(),
                key=lambda item: (item[1], item[0]),
                reverse=True,
            )
            if end == -1:
                end = len(values) - 1
            return [member.encode() for member, _ in values[start : end + 1]]

    def zrem(self, name: str, *values: Any) -> int:
        with self._lock:
            if self._types.get(name) not in (None, "zset"):
                raise TypeError(f"WRONGTYPE ZREM {name}")
            members = self._sorted_sets.get(name, {})
            removed = 0
            for value in values:
                member = value.decode() if isinstance(value, bytes) else str(value)
                removed += int(members.pop(member, None) is not None)
            return removed

    @staticmethod
    def _payload(record: dict[str, Any]) -> bytes:
        return json.dumps(_from_cjson(record), separators=(",", ":"), sort_keys=True).encode()

    def set_wrong_type(self, name: str, redis_type: str = "hash") -> None:
        with self._lock:
            self._types[name] = redis_type

    def add_stale_index_member(self, name: str, task_id: str, score: float) -> None:
        with self._lock:
            self._types[name] = "zset"
            self._sorted_sets.setdefault(name, {})[task_id] = score

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> list[Any]:
        with self._lock:
            keys = [str(value) for value in keys_and_args[:numkeys]]
            args = [
                value.decode() if isinstance(value, bytes) else str(value)
                for value in keys_and_args[numkeys:]
            ]

            if "create-v2" in script or "create-v1" in script:
                job_key, index_key = keys
                self._purge(job_key)
                if self._types.get(job_key) not in (None, "string"):
                    return [-2, ""]
                if job_key in self._values:
                    return [0, self._values[job_key]]
                if self._types.get(index_key) not in (None, "zset"):
                    return [-3, ""]
                self._values[job_key] = args[0].encode()
                self._types[job_key] = "string"
                if self.fail_next_zadd:
                    # Model SET succeeding, ZADD failing, and the Lua
                    # compensation deleting the just-created record.
                    self.fail_next_zadd = False
                    self._values.pop(job_key, None)
                    self._types.pop(job_key, None)
                    return [-4, ""]
                self._types[index_key] = "zset"
                self._sorted_sets.setdefault(index_key, {})[args[2]] = float(args[1])
                return [1, self._values[job_key]]

            job_key = keys[0]
            self._purge(job_key)
            payload = self._values.get(job_key)
            if payload is None:
                return [0, ""]
            record = _as_cjson(json.loads(payload))

            if "transition-v1" in script:
                expected, target, now, update_json, transitions_json, ttl, terminal = args
                if expected and record["state"] != expected:
                    return [-1, payload]
                if (
                    target == jobs.TrainingJobState.RUNNING.value
                    and record["state"] == jobs.TrainingJobState.QUEUED.value
                    and _has_json_value(record.get("start_deadline"))
                    and record["start_deadline"] <= now
                ):
                    record["state"] = jobs.TrainingJobState.TIMED_OUT.value
                    record["finished_at"] = now
                    record["heartbeat_at"] = now
                    record["error"] = {
                        "code": "start_deadline_exceeded",
                        "message": "Training did not start before its deadline.",
                    }
                    self._values[job_key] = self._payload(record)
                    self._types[job_key] = "string"
                    self._expires[job_key] = self._time() + int(ttl)
                    return [-3, self._values[job_key]]
                transitions = json.loads(transitions_json)
                if target not in transitions.get(record["state"], []):
                    return [-2, payload]
                _apply_updates(record, update_json)
                if target == jobs.TrainingJobState.RUNNING.value:
                    record["attempt"] = record.get("attempt", 0) + 1
                record["state"] = target
                self._values[job_key] = self._payload(record)
                self._types[job_key] = "string"
                if terminal == "1":
                    self._expires[job_key] = self._time() + int(ttl)
                return [1, self._values[job_key]]

            if "timeout-v1" in script:
                now, update_json, ttl = args
                if record["state"] != jobs.TrainingJobState.QUEUED.value:
                    return [2, payload]
                if (
                    not _has_json_value(record.get("start_deadline"))
                    or record["start_deadline"] > now
                ):
                    return [3, payload]
                _apply_updates(record, update_json)
                record["state"] = jobs.TrainingJobState.TIMED_OUT.value
                self._values[job_key] = self._payload(record)
                self._types[job_key] = "string"
                self._expires[job_key] = self._time() + int(ttl)
                return [1, self._values[job_key]]

            if "cancel-v1" in script:
                now, ttl = args
                state = record["state"]
                if state in {state.value for state in jobs.TERMINAL_STATES}:
                    return [2, payload]
                if state == jobs.TrainingJobState.CANCEL_REQUESTED.value:
                    return [2, payload]
                if state not in {
                    jobs.TrainingJobState.QUEUED.value,
                    jobs.TrainingJobState.RUNNING.value,
                    jobs.TrainingJobState.REGISTERING.value,
                }:
                    return [-2, payload]
                record["cancellation_requested_at"] = now
                record["heartbeat_at"] = now
                terminal = state == jobs.TrainingJobState.QUEUED.value
                record["state"] = (
                    jobs.TrainingJobState.CANCELLED.value
                    if terminal
                    else jobs.TrainingJobState.CANCEL_REQUESTED.value
                )
                if terminal:
                    record["finished_at"] = now
                self._values[job_key] = self._payload(record)
                self._types[job_key] = "string"
                if terminal:
                    self._expires[job_key] = self._time() + int(ttl)
                return [1, self._values[job_key]]

            if "patch-v1" in script:
                expected, _, update_json, terminals_json = args
                if expected and record["state"] != expected:
                    return [-1, payload]
                if _as_cjson(json.loads(terminals_json)).get(record["state"]):
                    return [-2, payload]
                _apply_updates(record, update_json)
                self._values[job_key] = self._payload(record)
                self._types[job_key] = "string"
                return [1, self._values[job_key]]

            raise AssertionError(f"unknown script in fake: {script[:80]}")


@pytest.fixture
def lifecycle() -> tuple[MemoryRedis, FrozenClock, jobs.TrainingJobStore]:
    redis = MemoryRedis()
    clock = FrozenClock()
    store = jobs.TrainingJobStore(
        redis,
        clock=clock,
        key_prefix="test:training-jobs",
        terminal_ttl_seconds=120,
        list_batch_size=2,
    )
    return redis, clock, store


def enqueue(
    store: jobs.TrainingJobStore,
    task_id: str = "task-1",
    **kwargs: Any,
) -> jobs.TrainingJob:
    return store.enqueue(
        task_id=task_id,
        dataset_id=kwargs.pop("dataset_id", 7),
        user_id=kwargs.pop("user_id", 11),
        model_id=kwargs.pop("model_id", "mask2former-v1"),
        **kwargs,
    )


def test_enqueue_persists_typed_record_and_task_identity(lifecycle):
    _, clock, store = lifecycle
    job = enqueue(
        store,
        task_id="celery-uuid-1",
        start_deadline=clock.value + timedelta(minutes=5),
        run_name="review run",
    )

    assert isinstance(job, jobs.TrainingJob)
    assert job.task_id == "celery-uuid-1"
    assert job.celery_task_id == job.task_id
    assert job.state is jobs.TrainingJobState.QUEUED
    assert job.dataset_id == 7
    assert job.queued_at == START
    assert store.get(job.task_id) == job
    with pytest.raises(jobs.TrainingJobAlreadyExists):
        enqueue(store, task_id=job.task_id)


def test_default_enqueue_start_and_timeout_scripts_handle_nullable_fields(lifecycle):
    _, _, store = lifecycle

    queued = enqueue(store, task_id="default-nullable")
    assert queued.start_deadline is None
    assert store.timeout_if_expired(queued.task_id).state is jobs.TrainingJobState.QUEUED

    running = store.mark_running(queued.task_id)
    assert running.state is jobs.TrainingJobState.RUNNING
    assert store.timeout_if_expired(queued.task_id).state is jobs.TrainingJobState.RUNNING


def test_transition_script_handles_explicit_null_update_fields(lifecycle):
    _, _, store = lifecycle
    enqueue(store, task_id="explicit-null-update", run_name="temporary name")

    running = store.transition(
        "explicit-null-update",
        jobs.TrainingJobState.RUNNING,
        expected_state=jobs.TrainingJobState.QUEUED,
        run_name=None,
    )

    assert running.run_name is None


def test_create_wrong_type_index_does_not_leave_an_orphan_record(lifecycle):
    redis, _, store = lifecycle
    redis.set_wrong_type(store.dataset_index_key(7), "hash")

    with pytest.raises(jobs.TrainingJobStoreError, match="incompatible key type"):
        enqueue(store, task_id="wrong-index-type")

    assert redis.get(store.job_key("wrong-index-type")) is None


def test_create_wrong_type_job_key_does_not_write_or_index(lifecycle):
    redis, _, store = lifecycle
    redis.set_wrong_type(store.job_key("wrong-job-type"), "list")

    with pytest.raises(jobs.TrainingJobStoreError, match="incompatible key type"):
        enqueue(store, task_id="wrong-job-type")

    assert store.dataset_index_key(7) not in redis._sorted_sets


def test_create_index_operation_failure_compensates_the_record(lifecycle):
    redis, _, store = lifecycle
    redis.fail_next_zadd = True

    with pytest.raises(jobs.TrainingJobStoreError, match="failed atomically"):
        enqueue(store, task_id="index-write-failure")

    assert redis.get(store.job_key("index-write-failure")) is None
    assert "index-write-failure" not in redis._sorted_sets.get(
        store.dataset_index_key(7), {}
    )


def test_allowed_transitions_reject_stale_or_terminal_writes_and_apply_ttl(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store)

    running = store.mark_running("task-1")
    assert running.state is jobs.TrainingJobState.RUNNING
    assert running.attempt == 1
    registering = store.mark_registering("task-1")
    assert registering.state is jobs.TrainingJobState.REGISTERING
    succeeded = store.mark_succeeded("task-1", mlflow_run_id="run-1")

    assert succeeded.state is jobs.TrainingJobState.SUCCEEDED
    assert succeeded.task_id == "task-1"
    assert succeeded.mlflow_run_id == "run-1"
    assert 0 < redis.ttl(store.job_key("task-1")) <= 120

    with pytest.raises(jobs.InvalidTrainingJobTransition) as exc_info:
        store.mark_running("task-1")
    assert exc_info.value.current_state is jobs.TrainingJobState.SUCCEEDED

    # A terminal record keeps its identity and snapshot until TTL expiry.
    assert store.get("task-1").task_id == "task-1"
    clock.advance(seconds=1)
    assert store.get("task-1").finished_at == START


def test_timeout_and_worker_start_have_one_atomic_winner(lifecycle):
    _, clock, store = lifecycle
    enqueue(store, start_deadline=START - timedelta(seconds=1))

    timed_out = store.timeout_if_expired("task-1")
    assert timed_out.state is jobs.TrainingJobState.TIMED_OUT
    assert timed_out.error.code == "start_deadline_exceeded"
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.mark_running("task-1")

    enqueue(store, task_id="task-2", start_deadline=START + timedelta(seconds=1))
    running = store.mark_running("task-2")
    assert running.state is jobs.TrainingJobState.RUNNING
    assert store.timeout_if_expired("task-2").state is jobs.TrainingJobState.RUNNING


def test_worker_claim_after_deadline_atomically_times_out_before_start(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store, start_deadline=START + timedelta(seconds=10))
    clock.advance(seconds=11)

    with pytest.raises(jobs.TrainingJobDeadlineExceeded) as exc_info:
        store.mark_running("task-1")

    assert exc_info.value.job.state is jobs.TrainingJobState.TIMED_OUT
    timed_out = store.require("task-1")
    assert timed_out.state is jobs.TrainingJobState.TIMED_OUT
    assert timed_out.error == jobs.TrainingJobError(
        code="start_deadline_exceeded",
        message="Training did not start before its deadline.",
    )
    assert 0 < redis.ttl(store.job_key("task-1")) <= 120


def test_cancellation_registration_race_has_one_terminal_winner(lifecycle):
    _, _, store = lifecycle
    enqueue(store)
    store.mark_running("task-1")
    store.mark_registering("task-1")

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []

    def cancel() -> None:
        barrier.wait()
        try:
            outcomes.append(("cancel", store.request_cancellation("task-1")))
        except Exception as exc:  # pragma: no cover - defensive race reporting
            outcomes.append(("cancel-error", exc))

    def succeed() -> None:
        barrier.wait()
        try:
            outcomes.append(("success", store.mark_succeeded("task-1")))
        except Exception as exc:
            outcomes.append(("success-error", exc))

    threads = [threading.Thread(target=cancel), threading.Thread(target=succeed)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    first = store.get("task-1")
    if first.state is jobs.TrainingJobState.CANCEL_REQUESTED:
        store.mark_cancelled("task-1")
        assert any(kind == "success-error" for kind, _ in outcomes)
    else:
        assert first.state is jobs.TrainingJobState.SUCCEEDED
        assert any(kind == "cancel" and value.state is first.state for kind, value in outcomes)

    assert store.get("task-1").state in {
        jobs.TrainingJobState.CANCELLED,
        jobs.TrainingJobState.SUCCEEDED,
    }


def test_queued_cancellation_is_terminal_and_late_worker_cannot_overwrite(lifecycle):
    redis, _, store = lifecycle
    enqueue(store)

    cancelled = store.request_cancellation("task-1")
    assert cancelled.state is jobs.TrainingJobState.CANCELLED
    assert cancelled.cancellation_requested_at == START
    assert 0 < redis.ttl(store.job_key("task-1")) <= 120
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.mark_running("task-1")


def test_running_cancellation_is_cooperative_and_gets_terminal_ttl(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store)
    store.mark_running("task-1")

    clock.advance(seconds=5)
    requested = store.request_cancellation("task-1")
    assert requested.state is jobs.TrainingJobState.CANCEL_REQUESTED
    assert requested.cancellation_requested_at == START + timedelta(seconds=5)

    clock.advance(seconds=5)
    cancelled = store.mark_cancelled("task-1")
    assert cancelled.state is jobs.TrainingJobState.CANCELLED
    assert cancelled.finished_at == START + timedelta(seconds=10)
    assert cancelled.heartbeat_at == START + timedelta(seconds=10)
    assert 0 < redis.ttl(store.job_key("task-1")) <= 120


def test_illegal_transitions_are_rejected_from_each_lifecycle_phase(lifecycle):
    _, _, store = lifecycle

    enqueue(store, task_id="illegal-queued")
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.transition("illegal-queued", jobs.TrainingJobState.REGISTERING)

    enqueue(store, task_id="illegal-running")
    store.mark_running("illegal-running")
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.transition("illegal-running", jobs.TrainingJobState.SUCCEEDED)

    enqueue(store, task_id="illegal-registering")
    store.mark_running("illegal-registering")
    store.mark_registering("illegal-registering")
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.transition("illegal-registering", jobs.TrainingJobState.RUNNING)

    enqueue(store, task_id="illegal-cancel-requested")
    store.mark_running("illegal-cancel-requested")
    store.request_cancellation("illegal-cancel-requested")
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.transition("illegal-cancel-requested", jobs.TrainingJobState.FAILED)

    enqueue(store, task_id="illegal-terminal")
    store.request_cancellation("illegal-terminal")
    with pytest.raises(jobs.InvalidTrainingJobTransition):
        store.transition("illegal-terminal", jobs.TrainingJobState.RUNNING)


def test_timeout_and_worker_start_race_has_one_consistent_winner(lifecycle):
    _, _, store = lifecycle
    enqueue(store, task_id="timeout-race", start_deadline=START - timedelta(seconds=1))
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []

    def timeout() -> None:
        barrier.wait()
        try:
            outcomes.append(("timeout", store.timeout_if_expired("timeout-race")))
        except Exception as exc:  # pragma: no cover - defensive race reporting
            outcomes.append(("timeout-error", exc))

    def start() -> None:
        barrier.wait()
        try:
            outcomes.append(("start", store.mark_running("timeout-race")))
        except Exception as exc:  # pragma: no cover - defensive race reporting
            outcomes.append(("start-error", exc))

    threads = [threading.Thread(target=timeout), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = store.require("timeout-race")
    assert final.state in {
        jobs.TrainingJobState.TIMED_OUT,
        jobs.TrainingJobState.RUNNING,
    }
    if final.state is jobs.TrainingJobState.TIMED_OUT:
        assert any(kind == "start-error" for kind, _ in outcomes)
    else:
        assert any(kind == "timeout" and value.state is final.state for kind, value in outcomes)


def test_queued_cancel_and_worker_start_race_preserves_single_state_machine_path(lifecycle):
    _, _, store = lifecycle
    enqueue(store, task_id="cancel-start-race")
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, Any]] = []

    def cancel() -> None:
        barrier.wait()
        try:
            outcomes.append(("cancel", store.request_cancellation("cancel-start-race")))
        except Exception as exc:  # pragma: no cover - defensive race reporting
            outcomes.append(("cancel-error", exc))

    def start() -> None:
        barrier.wait()
        try:
            outcomes.append(("start", store.mark_running("cancel-start-race")))
        except Exception as exc:  # pragma: no cover - defensive race reporting
            outcomes.append(("start-error", exc))

    threads = [threading.Thread(target=cancel), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final = store.require("cancel-start-race")
    if final.state is jobs.TrainingJobState.CANCEL_REQUESTED:
        store.mark_cancelled("cancel-start-race")
        final = store.require("cancel-start-race")
        assert any(kind == "start" for kind, _ in outcomes)
    assert final.state in {
        jobs.TrainingJobState.CANCELLED,
        jobs.TrainingJobState.RUNNING,
    }
    if final.state is jobs.TrainingJobState.CANCELLED:
        assert any(kind == "cancel" for kind, _ in outcomes)
    else:
        assert any(
            kind == "cancel"
            and value.state is jobs.TrainingJobState.CANCEL_REQUESTED
            for kind, value in outcomes
        )


def test_dataset_index_lists_newest_jobs_and_prunes_expired_terminal_members(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store, task_id="dataset-7-old", created_at=START)
    clock.advance(minutes=1)
    enqueue(store, task_id="dataset-7-new", created_at=clock.value, dataset_id=7)
    enqueue(store, task_id="dataset-8", created_at=clock.value, dataset_id=8)

    assert [job.task_id for job in store.list_for_dataset(7)] == [
        "dataset-7-new",
        "dataset-7-old",
    ]
    assert [job.task_id for job in store.list_for_dataset(7, limit=1)] == ["dataset-7-new"]
    assert store.list_for_dataset(999) == []

    store.request_cancellation("dataset-7-new")
    redis.advance(121)
    assert store.get("dataset-7-new") is None
    assert [job.task_id for job in store.list_for_dataset(7)] == ["dataset-7-old"]


def test_listing_paginates_and_prunes_stale_members_before_applying_offset(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store, task_id="page-old", created_at=START)
    clock.advance(minutes=1)
    enqueue(store, task_id="page-new", created_at=clock.value)
    redis.add_stale_index_member(
        store.dataset_index_key(7), "expired-before-page", START.timestamp() + 100
    )

    page = store.list_for_dataset(7, limit=1, offset=1)

    assert [job.task_id for job in page] == ["page-old"]
    assert "expired-before-page" not in redis._sorted_sets[store.dataset_index_key(7)]
    assert redis.range_calls
    assert all(end != -1 for _, _, end in redis.range_calls)
    assert all(end - start + 1 <= store.list_batch_size for _, start, end in redis.range_calls)


def test_listing_repairs_a_member_indexed_under_the_wrong_dataset(lifecycle):
    redis, _, store = lifecycle
    enqueue(store, task_id="wrong-dataset-index", dataset_id=8)
    redis.add_stale_index_member(
        store.dataset_index_key(7), "wrong-dataset-index", START.timestamp() + 1
    )

    assert store.list_for_dataset(7) == []
    assert "wrong-dataset-index" not in redis._sorted_sets[store.dataset_index_key(7)]
    assert store.get("wrong-dataset-index").dataset_id == 8


def test_progress_heartbeat_is_typed_and_terminal_jobs_cannot_be_patched(lifecycle):
    _, clock, store = lifecycle
    enqueue(store)
    store.mark_running("task-1")
    clock.advance(seconds=30)
    updated = store.update_progress(
        "task-1",
        epoch=3,
        total_epochs=10,
        loss=0.25,
        progress=0.3,
    )
    assert updated.epoch == 3
    assert updated.total_epochs == 10
    assert updated.loss == pytest.approx(0.25)
    assert updated.progress == pytest.approx(0.3)
    assert updated.heartbeat_at == START + timedelta(seconds=30)

    store.mark_registering("task-1")
    store.mark_succeeded("task-1")
    with pytest.raises(jobs.InvalidTrainingJobUpdate):
        store.update_progress("task-1", epoch=4)


def test_failure_serializes_safe_error_and_hides_server_traceback(lifecycle):
    _, _, store = lifecycle
    enqueue(store)
    error = RuntimeError("password=do-not-show /secret/project/config")

    failed = store.mark_failed("task-1", error)
    assert failed.state is jobs.TrainingJobState.FAILED
    assert failed.error == jobs.TrainingJobError(
        code="training_failed",
        message="Training failed before completion.",
    )
    assert "password=do-not-show" in failed.error_traceback
    public = failed.to_public_dict()
    assert "error_traceback" not in public
    assert "error_traceback" not in failed.model_dump()
    assert "password=do-not-show" not in json.dumps(public)
    assert jobs.serialize_error(error) == {
        "code": "training_failed",
        "message": "Training failed before completion.",
    }
    assert jobs.serialize_error("password=do-not-show") == {
        "code": "training_failed",
        "message": "Training failed before completion.",
    }
    assert jobs.serialize_error("internal detail", message="Safe public detail") == {
        "code": "training_failed",
        "message": "Safe public detail",
    }


def test_mapping_error_message_is_never_exposed_without_explicit_safe_override():
    assert jobs.serialize_error(
        {"code": "validation_error", "message": "secret=/etc/app/password"}
    ) == {
        "code": "validation_error",
        "message": "The training configuration is invalid.",
    }
    assert jobs.serialize_error(
        {"code": "unknown_internal_code", "message": "do-not-show"}
    ) == {
        "code": "unknown_internal_code",
        "message": "Training failed before completion.",
    }
    assert jobs.serialize_error(
        {"code": "validation_error", "message": "do-not-show"},
        message="The selected labels are invalid.",
    ) == {
        "code": "validation_error",
        "message": "The selected labels are invalid.",
    }


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_message"),
    [
        (TimeoutError("private timeout detail"), "timeout", "Training infrastructure timed out."),
        (
            ValueError("private validation detail"),
            "validation_error",
            "The training configuration is invalid.",
        ),
        (
            MemoryError("private OOM detail"),
            "resource_exhausted",
            "Training ran out of available resources.",
        ),
        (
            RuntimeError("private unknown detail"),
            "training_failed",
            "Training failed before completion.",
        ),
    ],
)
def test_default_failure_code_classification_is_used(
    lifecycle,
    error,
    expected_code,
    expected_message,
):
    _, _, store = lifecycle
    task_id = f"classify-{expected_code}"
    enqueue(store, task_id=task_id)

    failed = store.mark_failed(task_id, error)

    assert failed.error == jobs.TrainingJobError(
        code=expected_code,
        message=expected_message,
    )


def test_timeout_ttl_is_injected_configuration_and_clock_is_deterministic(lifecycle):
    redis, clock, store = lifecycle
    enqueue(store, start_deadline=START + timedelta(seconds=10))
    assert store.timeout_if_expired("task-1").state is jobs.TrainingJobState.QUEUED
    clock.advance(seconds=10)
    assert store.timeout_if_expired("task-1").state is jobs.TrainingJobState.TIMED_OUT
    assert 0 < redis.ttl(store.job_key("task-1")) <= 120


def test_store_rejects_invalid_terminal_ttl_and_empty_normalised_prefix():
    redis = MemoryRedis()
    with pytest.raises(ValueError):
        jobs.TrainingJobStore(redis, key_prefix=" ::: ")
    with pytest.raises(ValueError):
        jobs.TrainingJobStore(redis, terminal_ttl_seconds=1.5)
