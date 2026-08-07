"""Regression tests for the instance-segmentation task-tag check.

``iquana_toolbox.mlflow.MLFlowModelRegistry._model_info_tags`` always writes the
canonical underscore spelling (``task=instance_segmentation``) to both ``task``
and ``model_task``. ``validate_model`` previously compared against a hyphenated
spelling that the toolbox never writes, so every real instance-segmentation
model was incorrectly rejected with a 400.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from iquana_toolbox.schemas.networking.http.services import InstanceSegmentationRequest
from iquana_toolbox.schemas.training import InstanceSegmentationTrainingRequest

from app.state import MODEL_REGISTRY
from util.validate_model import _declares_instance_segmentation, validate_model


def _register_model(monkeypatch, tags: dict):
    registered_model = SimpleNamespace(tags=tags)
    monkeypatch.setattr(
        MODEL_REGISTRY.client, "get_registered_model", lambda name: registered_model
    )


def test_declares_instance_segmentation_matches_canonical_underscore_tag():
    assert _declares_instance_segmentation({"task": "instance_segmentation"})


def test_declares_instance_segmentation_tolerates_hyphenated_spelling():
    assert _declares_instance_segmentation({"task": "instance-segmentation"})


def test_declares_instance_segmentation_falls_back_to_model_task():
    assert _declares_instance_segmentation({"model_task": "instance_segmentation"})


def test_declares_instance_segmentation_rejects_other_tasks():
    assert not _declares_instance_segmentation({"task": "semantic_segmentation"})
    assert not _declares_instance_segmentation({})


def test_validate_model_accepts_real_registered_tags(monkeypatch):
    # Shape actually written by MLFlowModelRegistry._model_info_tags for a
    # trainable instance-segmentation model.
    _register_model(monkeypatch, {
        "task": "instance_segmentation",
        "model_task": "instance_segmentation",
        "trainable": "true",
    })
    request = InstanceSegmentationTrainingRequest(
        dataset_id=1,
        image_folder_path="/tmp/images",
        model_registry_key="mask2former",
        user_id="tester",
        labels=[],
        annotation_file_url="/tmp/annotations.json",
    )
    validate_model(request)  # must not raise


def test_validate_model_rejects_non_instance_segmentation_model(monkeypatch):
    _register_model(monkeypatch, {"task": "semantic_segmentation"})
    request = InstanceSegmentationRequest(
        image_url="/tmp/image.png",
        model_registry_key="some-model",
        user_id="tester",
    )
    with pytest.raises(HTTPException) as exc_info:
        validate_model(request)
    assert exc_info.value.status_code == 400
