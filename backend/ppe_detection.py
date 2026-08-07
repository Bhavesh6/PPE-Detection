import os

import cv2
import torch
from ultralytics import YOLO

MODEL_PATH = os.environ.get("MODEL_PATH", os.path.join(os.path.dirname(__file__), "..", "best.pt"))

VIOLATION_PREFIX = "NO-"
HELMET_CLASSES = {"Hardhat", "helmet"}
VEST_CLASSES = {"Safety Vest", "vest"}
GLOVE_CLASSES = {"Gloves", "hand gloves"}

BOX_COLORS = {
    "helmet": (0, 255, 0),
    "vest": (0, 165, 255),
    "gloves": (255, 0, 255),
    "violation": (0, 0, 255),
    "other": (255, 255, 0),
}


def load_model():
    """Load the YOLOv8 PPE detection model.

    PyTorch >=2.6 defaults torch.load(weights_only=True), which rejects the
    pickled Ultralytics checkpoint. We only trust our own best.pt, so we
    scope weights_only=False to this single load call rather than globally.
    """
    original_torch_load = torch.load

    def patched_torch_load(*args, **kwargs):
        kwargs["weights_only"] = False
        return original_torch_load(*args, **kwargs)

    torch.load = patched_torch_load
    try:
        model = YOLO(MODEL_PATH)
    finally:
        torch.load = original_torch_load

    return model


def _color_for_class(class_name):
    if class_name in HELMET_CLASSES:
        return BOX_COLORS["helmet"]
    if class_name in VEST_CLASSES:
        return BOX_COLORS["vest"]
    if class_name in GLOVE_CLASSES:
        return BOX_COLORS["gloves"]
    if class_name.startswith(VIOLATION_PREFIX):
        return BOX_COLORS["violation"]
    return BOX_COLORS["other"]


def process_frame(frame, model, draw=True, conf=0.25):
    """Run detection on a single BGR frame.

    Returns (annotated_frame_or_None, detections). Set draw=False to skip
    drawing boxes/labels when the caller only needs the detection list
    (e.g. an API endpoint that returns JSON to the browser, which draws
    the overlay itself).

    `conf` is the minimum confidence a detection needs to count. It's a
    parameter rather than a constant because it's site policy: raising it
    suppresses false violations at the cost of missing marginal real ones.
    """
    if frame is None or model is None:
        return None, []

    results = model(frame, conf=conf, iou=0.45, verbose=False)

    detections = []
    for result in results:
        for box in result.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            class_name = result.names[cls]

            detections.append({
                "type": class_name,
                "detected": True,
                "confidence": conf,
                "box": [x1, y1, x2, y2],
            })

            if draw:
                color = _color_for_class(class_name)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                label = f"{class_name} {conf:.2f}"
                cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

    return frame, detections


if __name__ == "__main__":
    # Local webcam smoke test: python ppe_detection.py
    model = load_model()
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise SystemExit("Could not open webcam")

    print("Press 'q' to quit")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        annotated, _ = process_frame(frame, model)
        cv2.imshow("PPE Detection", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
