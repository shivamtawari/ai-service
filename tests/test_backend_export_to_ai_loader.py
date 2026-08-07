import os
import sys
import json
import sys
import json
import subprocess
import cv2
import numpy as np

backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend"))

from models.mask2former_dataset import CocoInstanceDataset, LabelMapping

def _rect(height, width, top, left, bottom, right):
    mask = np.zeros((height, width), dtype=bool)
    mask[top:bottom, left:right] = True
    return mask

def _node(contour_id, label_id, mask, parent_id=None, image_id=1):
    node = {
        "id": contour_id,
        "label_id": label_id,
        "parent_id": parent_id,
        "mask": mask,
    }
    if image_id is not None:
        node["image_id"] = image_id
    return node

class DummyLabel:
    def __init__(self, id, name):
        self.id = id
        self.name = name

def test_backend_export_is_compatible_with_ai_loader(tmp_path):
    """
    Test that the backend's export function produces a payload that
    Mask2Former's CocoInstanceDataset natively parses without errors.
    """
    # 1. Ask backend to generate a dummy hierarchy payload via subprocess
    script = """
import json
import numpy as np
from app.services.instance_segmentation_training import encode_exclusive_hierarchy_v1

def _rect(height, width, top, left, bottom, right):
    mask = np.zeros((height, width), dtype=bool)
    mask[top:bottom, left:right] = True
    return mask

def _node(contour_id, label_id, mask, parent_id=None, image_id=1):
    node = {
        "id": contour_id,
        "label_id": label_id,
        "parent_id": parent_id,
        "mask": mask,
    }
    if image_id is not None:
        node["image_id"] = image_id
    return node

parent = _rect(64, 64, 10, 10, 50, 50)
child = _rect(64, 64, 20, 20, 40, 40)

label_parent_ids = {10: None, 20: 10}
payload = encode_exclusive_hierarchy_v1(
    [
        _node(101, 10, parent, image_id=7),
        _node(102, 20, child, parent_id=101, image_id=7)
    ],
    width=64,
    height=64,
    label_parent_ids=label_parent_ids,
    selected_label_ids=[10, 20],
    image_id=7,
)
payload["images"][0]["file_name"] = "image-7.png"
print(json.dumps(payload))
"""
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = env.get("UV_CACHE_DIR", "/tmp/uv-cache")

    result = subprocess.run(
        ["uv", "run", "python", "-c", script],
        cwd=backend_path,
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    
    # 2. Write payload and dummy image
    image = np.zeros((64, 64, 3), dtype=np.uint8)
    cv2.imwrite(str(tmp_path / "image-7.png"), image)
    
    annotation_path = tmp_path / "annotations.json"
    with open(annotation_path, "w") as f:
        json.dump(payload, f)
        
    # 3. Create LabelMapping
    labels = [DummyLabel(10, "parent"), DummyLabel(20, "child")]
    mapping = LabelMapping.from_selected_labels(labels)
    
    # 4. Load with CocoInstanceDataset
    # It should not raise any CocoTrainingDataError or KeyError.
    dataset = CocoInstanceDataset(
        annotation_file=annotation_path,
        image_folder=tmp_path,
        label_mapping=mapping,
    )
    
    # Verify metadata was parsed
    assert dataset.hierarchy_version == "exclusive_hierarchy_v1"
    assert dataset.label_parent_ids == {"10": None, "20": 10}
    
    # Verify we can get an item
    sample = dataset[0]
    
    # Should have two disjoint instances + ignore index (255)
    unique_ids = set(np.unique(sample.instance_mask))
    assert unique_ids == {1, 2, 255}
