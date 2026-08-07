import numpy as np
from scipy import ndimage

def compute_containment(child_mask: np.ndarray, parent_mask: np.ndarray) -> float:
    """Return the fraction of child_mask that falls within parent_mask."""
    child_area = np.count_nonzero(child_mask)
    if child_area == 0:
        return 0.0
    intersection = np.count_nonzero(child_mask & parent_mask)
    return intersection / child_area

def decode_exclusive_hierarchy(
    predictions: list[dict],
    label_to_parent: dict[int, int | None],
    containment_threshold: float = 0.5,
) -> list[dict]:
    """Reconstruct hierarchical masks from mutually exclusive predictions.

    Args:
        predictions: list of dicts with:
            "id": stable prediction ID (int)
            "label_id": database label ID (int)
            "score": confidence score (float)
            "binary_mask": boolean numpy array of the prediction
        label_to_parent: mapping from database label ID to its parent database label ID (or None).
        containment_threshold: minimum containment fraction to assign a child to a parent.

    Returns:
        list of the same dicts, but with "binary_mask" updated to the reconstructed geometry,
        and "parent_prediction_id" set to the chosen parent's ID (or None).
    """
    # Group predictions by label ID
    preds_by_label = {}
    for p in predictions:
        preds_by_label.setdefault(p["label_id"], []).append(p)
        p["children"] = []
        p["parent_prediction_id"] = None
        p["reconstructed_mask"] = p["binary_mask"].copy()

    # Determine processing order: deepest to root.
    # Build tree of labels to find depths.
    children_of_label = {}
    for lbl, p_lbl in label_to_parent.items():
        children_of_label.setdefault(p_lbl, []).append(lbl)

    def get_depth(lbl):
        d = 0
        curr = label_to_parent.get(lbl)
        while curr is not None:
            d += 1
            curr = label_to_parent.get(curr)
        return d

    unique_labels = list(label_to_parent.keys())
    unique_labels.sort(key=get_depth, reverse=True)

    # For each label from deepest up to just below root
    for child_label in unique_labels:
        parent_label = label_to_parent.get(child_label)
        if parent_label is None:
            continue

        child_preds = preds_by_label.get(child_label, [])
        parent_preds = preds_by_label.get(parent_label, [])

        for child in child_preds:
            best_parent = None
            best_containment = -1.0

            # Tie-breaking: score (descending), then prediction ID (ascending)
            def sort_key(p):
                return (-p["score"], p["id"])
            
            sorted_parents = sorted(parent_preds, key=sort_key)

            for parent in sorted_parents:
                # Build temporary parent envelope from residual + assigned children
                # Then fill holes so we can check if the child is contained inside it.
                parent_envelope = ndimage.binary_fill_holes(parent["reconstructed_mask"])
                containment = compute_containment(child["reconstructed_mask"], parent_envelope)
                
                if containment >= containment_threshold and containment > best_containment:
                    best_containment = containment
                    best_parent = parent
            
            if best_parent is not None:
                child["parent_prediction_id"] = best_parent["id"]
                best_parent["children"].append(child)
                best_parent["reconstructed_mask"] |= child["reconstructed_mask"]

    # Finalize masks
    for p in predictions:
        p["binary_mask"] = p["reconstructed_mask"]
        del p["reconstructed_mask"]
        del p["children"]

    return predictions
