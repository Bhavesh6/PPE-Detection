"""Live camera + on-device PPE detection, on the Pi's own screen."""
import sys, time
sys.path.insert(0, "/home/nexus/safetyfirst-checkpoint/pi_app")
import cv2, onnx_detector as od

COLORS = {
    "Hardhat": (0, 220, 0), "Safety Vest": (0, 220, 0), "Mask": (0, 220, 0),
    "NO-Hardhat": (0, 0, 255), "NO-Safety Vest": (0, 0, 255), "NO-Mask": (0, 0, 255),
    "Person": (255, 170, 0),
}
REQUIRED = ["Hardhat", "Safety Vest"]

det = od.OnnxDetector("/home/nexus/best.onnx", conf=0.25, iou=0.45, threads=4)
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
if not cap.isOpened():
    print("could not open camera"); sys.exit(1)

win = "SafetyFirst — on-device PPE detection"
cv2.namedWindow(win, cv2.WINDOW_NORMAL)
cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

print("[viewer] running — press q on the Pi to quit (auto-exits after 10 min)")
started, fps, frames, t_fps = time.time(), 0.0, 0, time.time()

while time.time() - started < 600:
    ok, frame = cap.read()
    if not ok:
        continue

    dets = det.detect(frame)
    for d in dets:
        x1, y1, x2, y2 = d["box"]
        colour = COLORS.get(d["type"], (200, 200, 200))
        cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(frame, f"{d['type']} {d['confidence']:.2f}", (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 2)

    present = {d["type"] for d in dets}
    if "Person" not in present:
        verdict, colour = "NO PERSON", (140, 140, 140)
    else:
        missing = [i for i in REQUIRED if f"NO-{i}" in present or i not in present]
        verdict = "DENIED - " + ", ".join(missing) if missing else "GRANTED"
        colour = (0, 0, 255) if missing else (0, 200, 0)

    frames += 1
    if frames >= 5:
        fps = frames / (time.time() - t_fps)
        frames, t_fps = 0, time.time()

    cv2.rectangle(frame, (0, 0), (frame.shape[1], 34), (20, 20, 20), -1)
    cv2.putText(frame, verdict, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, colour, 2)
    cv2.putText(frame, f"CPU {fps:.1f} FPS  no backend", (frame.shape[1] - 240, 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

    cv2.imshow(win, frame)
    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
print("[viewer] stopped")
