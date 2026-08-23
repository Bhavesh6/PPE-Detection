"""Run the PPE model on this Pi's CPU, with no backend and no accelerator.

The gate normally sends frames to the backend, which owns the model. That
is the right default — it keeps this install light and the policy in one
place. But it makes the gate useless without a network, and the Hailo
accelerator that was meant to solve that is blocked on a HEF/runtime
version mismatch that no amount of code here can fix.

ONNX Runtime is the third way, and it is already installed. Measured on
this Pi: 317ms per frame at 640x640, about 3.2 FPS on four cores. That
sounds slow and is not, for this job — the gate infers per badge scan,
not continuously, and checkpoint.py already throttles to one frame every
0.5s. A worker does not change their PPE thirty times a second.

Produces exactly the detection shape the backend returns, so whatever
consumes one can consume the other:

    {"type": "Hardhat", "detected": True, "confidence": 0.87,
     "box": [x1, y1, x2, y2]}
"""

from __future__ import annotations

import os

import cv2
import numpy as np

# YOLOv8 exports as [1, 4 + num_classes, 8400]: four box terms then one
# score per class, for every anchor.
BOX_TERMS = 4

# Matches the trained model's metadata.yaml. Order is the class index and
# must not be sorted or rearranged — a list in the wrong order mislabels
# every detection while looking entirely reasonable.
DEFAULT_CLASSES = [
    "Hardhat", "Mask", "NO-Hardhat", "NO-Mask", "NO-Safety Vest",
    "Person", "Safety Cone", "Safety Vest", "machinery", "vehicle",
]


def _letterbox(frame, size):
    """Resize keeping aspect ratio, padding the remainder.

    Returns the padded image plus what was done to it, because undoing
    exactly this is the only way a box in model space maps back to a box
    in camera space.
    """
    height, width = frame.shape[:2]
    target_h, target_w = size
    ratio = min(target_h / height, target_w / width)
    new_w, new_h = int(round(width * ratio)), int(round(height * ratio))

    resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_w, pad_h = (target_w - new_w) / 2, (target_h - new_h) / 2

    top, bottom = int(round(pad_h - 0.1)), int(round(pad_h + 0.1))
    left, right = int(round(pad_w - 0.1)), int(round(pad_w + 0.1))
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return padded, ratio, (left, top)


class OnnxDetector:
    """The PPE model, on the CPU."""

    name = "onnx (on-device CPU)"

    def __init__(self, model_path: str, classes: list[str] | None = None,
                 conf: float = 0.25, iou: float = 0.45, threads: int = 4):
        import onnxruntime as ort   # imported late: only needed in this mode

        options = ort.SessionOptions()
        # The Pi 5 has four cores and nothing else competing for them
        # during a badge check. Left at the default this runs on one.
        options.intra_op_num_threads = threads

        self._session = ort.InferenceSession(
            model_path, options, providers=["CPUExecutionProvider"])

        spec = self._session.get_inputs()[0]
        self._input_name = spec.name
        # Shape is [1, 3, H, W]; a dynamic axis comes back as a string, so
        # fall back to 640 rather than crashing on it.
        self._size = (
            spec.shape[2] if isinstance(spec.shape[2], int) else 640,
            spec.shape[3] if isinstance(spec.shape[3], int) else 640,
        )

        self.classes = classes or DEFAULT_CLASSES
        self.conf = conf
        self.iou = iou
        self.name = f"onnx {os.path.basename(model_path)} ({self._size[1]}x{self._size[0]}, CPU)"

    def detect(self, frame) -> list[dict]:
        """Detections for one BGR frame, in the backend's shape."""
        if frame is None or frame.size == 0:
            return []

        padded, ratio, (pad_x, pad_y) = _letterbox(frame, self._size)
        blob = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = blob.transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        raw = self._session.run(None, {self._input_name: blob})[0]
        return self._decode(raw, ratio, pad_x, pad_y, frame.shape[:2])

    def _decode(self, raw, ratio, pad_x, pad_y, original) -> list[dict]:
        # (1, 4+C, 8400) -> (8400, 4+C): one row per candidate box.
        predictions = np.squeeze(raw, 0).T
        if predictions.shape[1] <= BOX_TERMS:
            return []

        scores = predictions[:, BOX_TERMS:]
        class_ids = scores.argmax(axis=1)
        confidences = scores[np.arange(scores.shape[0]), class_ids]

        keep = confidences >= self.conf
        if not keep.any():
            return []

        boxes = predictions[keep, :BOX_TERMS]
        class_ids = class_ids[keep]
        confidences = confidences[keep]

        # Model space is centre/width/height on the padded image. Undo the
        # padding first, then the scale — in the other order the padding
        # would be divided by the ratio too and every box would drift.
        cx, cy, w, h = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
        x1 = (cx - w / 2 - pad_x) / ratio
        y1 = (cy - h / 2 - pad_y) / ratio
        x2 = (cx + w / 2 - pad_x) / ratio
        y2 = (cy + h / 2 - pad_y) / ratio

        height, width = original
        x1 = np.clip(x1, 0, width)
        y1 = np.clip(y1, 0, height)
        x2 = np.clip(x2, 0, width)
        y2 = np.clip(y2, 0, height)

        # NMS must be per class, and on this model that is not a detail.
        # A Hardhat sits almost entirely inside the Person wearing it, so
        # class-agnostic NMS would suppress the hardhat as a duplicate of
        # the person — and the gate would deny someone who is wearing one.
        # Offsetting each class into its own coordinate band gets per-class
        # behaviour out of a single NMS call.
        offsets = class_ids.astype(np.float32) * (max(width, height) + 1)
        nms_boxes = np.stack([x1 + offsets, y1 + offsets, x2 - x1, y2 - y1], axis=1)

        indices = cv2.dnn.NMSBoxes(
            nms_boxes.tolist(), confidences.astype(float).tolist(),
            float(self.conf), float(self.iou))
        if len(indices) == 0:
            return []
        indices = np.array(indices).flatten()

        detections = []
        for i in indices:
            class_id = int(class_ids[i])
            detections.append({
                "type": self.classes[class_id] if class_id < len(self.classes) else str(class_id),
                "detected": True,
                "confidence": float(confidences[i]),
                "box": [int(x1[i]), int(y1[i]), int(x2[i]), int(y2[i])],
            })
        return detections


def load_classes(path: str | None) -> list[str] | None:
    """Class names, one per line, or None to use the built-in list."""
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        names = [line.strip() for line in handle if line.strip()]
    return names or None


def open_detector() -> OnnxDetector | None:
    """Return a detector when one is configured, otherwise None.

    None rather than a do-nothing object: the caller prints what it got,
    and "inference happens on the backend" should read differently from
    "a local model that never detects anything".
    """
    model = os.environ.get("SAFETYFIRST_ONNX_MODEL", "").strip()
    if not model:
        return None
    if not os.path.isfile(model):
        print(f"[onnx] {model} not found — falling back to backend inference")
        return None

    try:
        return OnnxDetector(
            model,
            classes=load_classes(os.environ.get("SAFETYFIRST_ONNX_LABELS")),
            conf=float(os.environ.get("SAFETYFIRST_ONNX_CONF", "0.25")),
            iou=float(os.environ.get("SAFETYFIRST_ONNX_IOU", "0.45")),
            threads=int(os.environ.get("SAFETYFIRST_ONNX_THREADS", "4")),
        )
    except Exception as exc:  # noqa: BLE001 - missing runtime, bad model, etc.
        print(f"[onnx] could not start local inference ({exc}); using the backend")
        return None
