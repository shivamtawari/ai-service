"""COCO loading and label-space conversion for Mask2Former fine-tuning.

COCO category IDs originate in the IQUANA database and are therefore sparse.
Mask2Former expects contiguous semantic class indices, so this module owns the
lossless conversion between the two spaces and the validation of exported data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np
from torch.utils.data import Dataset

INSTANCE_IGNORE_INDEX = 255


class CocoTrainingDataError(ValueError):
    """Raised when an exported COCO training dataset is incomplete or invalid."""


@dataclass(frozen=True)
class LabelMapping:
    """Convert sparse database label IDs to contiguous model class indices."""

    database_to_model: dict[int, int]
    model_to_database: dict[int, int]
    id2label: dict[int, str]
    label_to_parent: dict[int, int | None] | None = None
    hierarchy_version: str | None = None

    @classmethod
    def from_selected_labels(cls, labels: Sequence[Any]) -> "LabelMapping":
        """Build a deterministic mapping from the selected IQUANA labels."""
        labels_by_id = {int(label.id): str(label.name) for label in labels}
        if not labels_by_id:
            raise CocoTrainingDataError("At least one label must be selected for training.")
        database_ids = sorted(labels_by_id)
        database_to_model = {
            database_id: model_index
            for model_index, database_id in enumerate(database_ids)
        }
        return cls(
            database_to_model=database_to_model,
            model_to_database={
                model_index: database_id
                for database_id, model_index in database_to_model.items()
            },
            id2label={
                model_index: labels_by_id[database_id]
                for database_id, model_index in database_to_model.items()
            },
        )
    
    def with_hierarchy(
        self,
        label_parent_ids: dict[str, int | None],
        hierarchy_version: str,
    ) -> "LabelMapping":
        """Return a new LabelMapping enriched with hierarchy information."""
        label_to_parent = {}
        for lbl_str, parent_id in label_parent_ids.items():
            label_to_parent[int(lbl_str)] = int(parent_id) if parent_id is not None else None

        return LabelMapping(
            database_to_model=self.database_to_model,
            model_to_database=self.model_to_database,
            id2label=self.id2label,
            label_to_parent=label_to_parent,
            hierarchy_version=hierarchy_version,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LabelMapping":
        """Restore a mapping persisted alongside a registered model artifact."""
        return cls(
            database_to_model={
                int(database_id): int(model_index)
                for database_id, model_index in payload["database_to_model"].items()
            },
            model_to_database={
                int(model_index): int(database_id)
                for model_index, database_id in payload["model_to_database"].items()
            },
            id2label={
                int(model_index): str(label_name)
                for model_index, label_name in payload["id2label"].items()
            },
            label_to_parent={
                int(label_id): (int(parent_id) if parent_id is not None else None)
                for label_id, parent_id in payload["label_to_parent"].items()
            } if payload.get("label_to_parent") is not None else None,
            hierarchy_version=payload.get("hierarchy_version"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation suitable for an MLflow artifact."""
        return {
            "database_to_model": self.database_to_model,
            "model_to_database": self.model_to_database,
            "id2label": self.id2label,
            "label_to_parent": self.label_to_parent,
            "hierarchy_version": self.hierarchy_version,
        }


@dataclass(frozen=True)
class CocoSample:
    """One decoded RGB image and its instance-id training target."""

    image: np.ndarray
    instance_mask: np.ndarray
    instance_id_to_semantic_id: dict[int, int]


def _decode_segmentation(segmentation: Any, height: int, width: int) -> np.ndarray:
    """Decode one COCO polygon or RLE segmentation into a binary mask."""
    mask = np.zeros((height, width), dtype=np.uint8)
    if isinstance(segmentation, list):
        polygons = segmentation
        if polygons and isinstance(polygons[0], (int, float)):
            polygons = [polygons]
        for polygon in polygons:
            points = np.asarray(polygon, dtype=np.float32)
            if points.size < 6 or points.size % 2:
                continue
            cv2.fillPoly(mask, [np.rint(points).astype(np.int32).reshape(-1, 1, 2)], 1)
        return mask
    if not isinstance(segmentation, dict):
        raise CocoTrainingDataError("COCO annotation has no polygon or RLE segmentation.")

    try:
        from pycocotools import mask as mask_utils

        rle = segmentation
        if isinstance(rle.get("counts"), list):
            rle = mask_utils.frPyObjects(rle, height, width)
        decoded = mask_utils.decode(rle)
    except (TypeError, ValueError, ImportError) as exc:
        raise CocoTrainingDataError("COCO RLE segmentation could not be decoded.") from exc
    if decoded.ndim == 3:
        decoded = np.any(decoded, axis=2)
    if decoded.shape != (height, width):
        raise CocoTrainingDataError("Decoded COCO RLE dimensions do not match its image.")
    return np.asarray(decoded, dtype=np.uint8)


class CocoInstanceDataset(Dataset[CocoSample]):
    """Validated selected-label COCO dataset for Mask2Former.

    parent contours are excluded by the gateway's old exporter.
    The new exclusive hierarchy exporter guarantees mutually exclusive pixels
    across all instances.
    """

    def __init__(
        self,
        annotation_file: str | Path,
        image_folder: str | Path,
        label_mapping: LabelMapping,
    ) -> None:
        """Load and validate selected-label COCO metadata before training starts."""
        annotation_path = Path(annotation_file)
        if not annotation_path.is_file():
            raise CocoTrainingDataError("COCO annotation file does not exist.")
        try:
            with annotation_path.open(encoding="utf-8") as annotation_stream:
                coco = json.load(annotation_stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise CocoTrainingDataError("COCO annotation file is not valid JSON.") from exc
        if not isinstance(coco, dict):
            raise CocoTrainingDataError("COCO annotation data must be a JSON object.")

        selected_ids = set(label_mapping.database_to_model)
        categories = coco.get("categories")
        images = coco.get("images")
        annotations = coco.get("annotations")
        if not all(isinstance(value, list) for value in (categories, images, annotations)):
            raise CocoTrainingDataError("COCO data must contain images, annotations, and categories lists.")
        
        self.hierarchy_version = coco.get("target_encoding")
        if self.hierarchy_version != "exclusive_hierarchy_v1":
            raise CocoTrainingDataError("Dataset is not exported in exclusive_hierarchy_v1 format.")
        
        hierarchy = coco.get("hierarchy")
        if not isinstance(hierarchy, dict) or not isinstance(hierarchy.get("label_parent_ids"), dict):
            raise CocoTrainingDataError("Dataset missing exclusive hierarchy sidecar.")
        self.label_parent_ids = hierarchy["label_parent_ids"]

        category_ids = {category.get("id") for category in categories if isinstance(category, dict)}
        missing_categories = selected_ids.difference(category_ids)
        if missing_categories:
            raise CocoTrainingDataError(
                f"Selected labels are absent from the COCO categories: {sorted(missing_categories)}."
            )

        self._image_folder = Path(image_folder)
        image_by_id = {
            image.get("id"): image
            for image in images
            if isinstance(image, dict) and image.get("id") is not None
        }
        annotations_by_image: dict[int, list[dict[str, Any]]] = {}
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if annotation.get("category_id") not in selected_ids:
                continue
            image_id = annotation.get("image_id")
            if image_id not in image_by_id:
                raise CocoTrainingDataError("COCO annotation references an unknown image.")
            annotations_by_image.setdefault(image_id, []).append(annotation)

        self._samples = [
            (image_by_id[image_id], image_annotations)
            for image_id, image_annotations in annotations_by_image.items()
            if image_annotations
        ]
        if not self._samples:
            raise CocoTrainingDataError("No training images remain after selected-label filtering.")

        # Verify per-label coverage across selected labels
        annotated_labels = {ann.get("category_id") for sample in self._samples for ann in sample[1] if ann.get("category_id") in selected_ids}
        unannotated = selected_ids - annotated_labels
        if unannotated:
            raise CocoTrainingDataError(f"Selected labels have zero annotations in the dataset: {sorted(unannotated)}.")

        self._label_mapping = label_mapping

    def __len__(self) -> int:
        """Return the number of images containing a selected-label instance."""
        return len(self._samples)

    def __getitem__(self, index: int) -> CocoSample:
        """Decode one image and construct its instance-id/semantic-class targets."""
        image_meta, annotations = self._samples[index]
        image_path = self._image_folder / str(image_meta["file_name"])
        image_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image_bgr is None:
            raise CocoTrainingDataError("A COCO training image could not be decoded.")
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        height, width = image_rgb.shape[:2]
        instance_mask = np.full((height, width), INSTANCE_IGNORE_INDEX, dtype=np.int32)
        instance_id_to_semantic_id: dict[int, int] = {}

        for instance_id, annotation in enumerate(annotations, start=1):
            if instance_id >= INSTANCE_IGNORE_INDEX:
                raise CocoTrainingDataError(
                    f"Image {image_meta.get('id')} has {len(annotations)} instances, which exceeds "
                    f"the maximum allowed instances per image ({INSTANCE_IGNORE_INDEX - 1})."
                )
            foreground = _decode_segmentation(annotation.get("segmentation"), height, width)
            if not np.any(foreground):
                continue
            
            # Verify mutually exclusive instances
            overlap = instance_mask[foreground > 0] != INSTANCE_IGNORE_INDEX
            if np.any(overlap):
                raise CocoTrainingDataError(
                    f"Overlapping annotations found in image {image_meta.get('id')}."
                    " Exclusive hierarchy requires mutually exclusive pixels."
                )


            instance_mask[foreground > 0] = instance_id
            instance_id_to_semantic_id[instance_id] = self._label_mapping.database_to_model[
                int(annotation["category_id"])
            ]

        if not instance_id_to_semantic_id:
            raise CocoTrainingDataError("A selected-label training image has no valid instance masks.")
        return CocoSample(
            image=image_rgb,
            instance_mask=instance_mask,
            instance_id_to_semantic_id=instance_id_to_semantic_id,
        )
