import numpy as np
import pytest

from models.hierarchy_decoder import compute_containment, decode_exclusive_hierarchy

def test_compute_containment():
    parent = np.zeros((10, 10), dtype=bool)
    parent[0:5, 0:5] = True
    
    child = np.zeros((10, 10), dtype=bool)
    child[2:4, 2:4] = True
    
    assert compute_containment(child, parent) == 1.0
    
    child2 = np.zeros((10, 10), dtype=bool)
    child2[4:6, 4:6] = True # half inside, half outside
    assert compute_containment(child2, parent) == 0.25

def test_decode_exclusive_hierarchy():
    label_to_parent = {
        10: None, # parent
        20: 10,   # child
        30: 20,   # grandchild
    }
    
    # Residuals (mutually exclusive)
    parent_mask = np.zeros((10, 10), dtype=bool)
    parent_mask[0:10, 0:10] = True
    parent_mask[2:8, 2:8] = False # hole for child
    
    child_mask = np.zeros((10, 10), dtype=bool)
    child_mask[2:8, 2:8] = True
    child_mask[4:6, 4:6] = False # hole for grandchild
    
    grandchild_mask = np.zeros((10, 10), dtype=bool)
    grandchild_mask[4:6, 4:6] = True
    
    predictions = [
        {"id": 1, "label_id": 10, "score": 0.9, "binary_mask": parent_mask},
        {"id": 2, "label_id": 20, "score": 0.8, "binary_mask": child_mask},
        {"id": 3, "label_id": 30, "score": 0.7, "binary_mask": grandchild_mask},
    ]
    
    decoded = decode_exclusive_hierarchy(predictions, label_to_parent, containment_threshold=0.5)
    
    # Assert reconstructed masks
    for p in decoded:
        if p["id"] == 1:
            assert np.all(p["binary_mask"] == True)
            assert p["parent_prediction_id"] is None
        elif p["id"] == 2:
            expected = np.zeros((10, 10), dtype=bool)
            expected[2:8, 2:8] = True
            assert np.array_equal(p["binary_mask"], expected)
            assert p["parent_prediction_id"] == 1
        elif p["id"] == 3:
            assert p["parent_prediction_id"] == 2

def test_decode_missing_parent():
    label_to_parent = {10: None, 20: 10}
    child_mask = np.zeros((10, 10), dtype=bool)
    child_mask[2:8, 2:8] = True
    
    predictions = [
        {"id": 2, "label_id": 20, "score": 0.8, "binary_mask": child_mask},
    ]
    
    decoded = decode_exclusive_hierarchy(predictions, label_to_parent)
    assert len(decoded) == 1
    assert decoded[0]["parent_prediction_id"] is None # child remains root

def test_decode_missing_child():
    label_to_parent = {10: None, 20: 10}
    parent_mask = np.zeros((10, 10), dtype=bool)
    parent_mask[0:10, 0:10] = True
    parent_mask[2:8, 2:8] = False
    
    predictions = [
        {"id": 1, "label_id": 10, "score": 0.9, "binary_mask": parent_mask},
    ]
    
    decoded = decode_exclusive_hierarchy(predictions, label_to_parent)
    assert len(decoded) == 1
    # parent remains its predicted residual
    assert np.array_equal(decoded[0]["binary_mask"], parent_mask)

def test_decode_ambiguous_equal_candidates():
    label_to_parent = {10: None, 20: 10}
    
    parent1_mask = np.zeros((10, 10), dtype=bool)
    parent1_mask[0:5, 0:5] = True
    
    parent2_mask = np.zeros((10, 10), dtype=bool)
    parent2_mask[5:10, 5:10] = True
    
    child_mask = np.zeros((10, 10), dtype=bool)
    child_mask[4:6, 4:6] = True # overlapping both equally (1 px in parent1, 1 px in parent2, 2 px empty)
    # wait, containment: 
    # child area = 4. intersection with parent1 = 1, parent2 = 1. Containment = 0.25 (less than 0.5 threshold)
    # Let's make containment exactly equal and above threshold.
    
    parent3_mask = np.zeros((10, 10), dtype=bool)
    parent3_mask[0:5, 0:10] = True
    parent4_mask = np.zeros((10, 10), dtype=bool)
    parent4_mask[5:10, 0:10] = True
    
    child2_mask = np.zeros((10, 10), dtype=bool)
    child2_mask[0:10, 0:1] = True # area 10. 5 in parent3, 5 in parent4. Containment = 0.5.
    
    predictions = [
        {"id": 1, "label_id": 10, "score": 0.9, "binary_mask": parent3_mask},
        {"id": 2, "label_id": 10, "score": 0.95, "binary_mask": parent4_mask}, # Higher score
        {"id": 3, "label_id": 20, "score": 0.8, "binary_mask": child2_mask},
    ]
    
    decoded = decode_exclusive_hierarchy(predictions, label_to_parent, containment_threshold=0.5)
    
    for p in decoded:
        if p["id"] == 3:
            # Should tie-break by score -> picks parent4 (id: 2)
            assert p["parent_prediction_id"] == 2
