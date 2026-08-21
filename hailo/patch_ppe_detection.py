"""Point ppe_detection.py at the exported model folder instead of guesses."""
import pathlib, shutil, sys

p = pathlib.Path.home() / "Downloads/construction-ppe-detection-main/ppe_detection.py"
src = p.read_text(encoding="utf-8")
shutil.copy(p, str(p) + ".bak")

edits = [
    # 1. Default to the exported folder, not a loose best.hef in cwd.
    ('default="best.hef",\n        help="Path to the compiled Hailo HEF model file (default: best.hef)"',
     'default=None,\n        help="Path to a .hef (default: best/best_hailo_model/best.hef)"'),

    # 2 & 3. Thresholds come from nms_config.json unless overridden. The
    # old defaults (0.45/0.45) silently disagreed with what the model was
    # compiled with (0.25/0.7).
    ('default=0.45,\n        help="Confidence threshold for detections (default: 0.45)"',
     'default=None,\n        help="Confidence threshold (default: from the model\'s nms_config.json)"'),
    ('default=0.45,\n        help="NMS IoU threshold (default: 0.45)"',
     'default=None,\n        help="NMS IoU threshold (default: from the model\'s nms_config.json)"'),

    # 4. Input size is read from the model rather than assumed square 640.
    ('# Default PPE class names (modify to match your model\'s classes)',
     'from model_config import load_model_config\n\n'
     '# Set from the model at startup; extract_detections() needs it to undo\n'
     '# the letterbox, and hardcoding 640 breaks silently on any other export.\n'
     'MODEL_INPUT = [640, 640]        # [height, width]\n\n'
     '# Fallback only — the real list comes from metadata.yaml.'),

    # 5. Use the model's own answers.
    ('    classes = load_classes(args.labels)\n'
     '    print(f"[INFO] Loaded {len(classes)} classes: {classes}")',
     '    model = load_model_config(hef_override=args.hef)\n'
     '    print(model.describe())\n'
     '    args.hef = model.hef\n'
     '    # An explicit --labels still wins; everything else defers to the\n'
     '    # export, so the script cannot drift from the model it loads.\n'
     '    classes = load_classes(args.labels) if args.labels else model.classes\n'
     '    if args.conf_thresh is None:\n'
     '        args.conf_thresh = model.conf_thresh\n'
     '    if args.iou_thresh is None:\n'
     '        args.iou_thresh = model.iou_thresh\n'
     '    MODEL_INPUT[0], MODEL_INPUT[1] = model.imgsz[0], model.imgsz[1]'),

    # 6. Undo the letterbox against the real input size.
    ('x1 = int(((xmin * 640) - dw) / ratio)', 'x1 = int(((xmin * MODEL_INPUT[1]) - dw) / ratio)'),
    ('y1 = int(((ymin * 640) - dh) / ratio)', 'y1 = int(((ymin * MODEL_INPUT[0]) - dh) / ratio)'),
    ('x2 = int(((xmax * 640) - dw) / ratio)', 'x2 = int(((xmax * MODEL_INPUT[1]) - dw) / ratio)'),
    ('y2 = int(((ymax * 640) - dh) / ratio)', 'y2 = int(((ymax * MODEL_INPUT[0]) - dh) / ratio)'),

    # 7. The model says lowercase; the colour table keyed on the old names
    #    would never have matched these two.
    ('    "Machinery",\n    "Vehicle"', '    "machinery",\n    "vehicle"'),
]

applied, missing = 0, []
for old, new in edits:
    if old in src:
        src = src.replace(old, new, 1)
        applied += 1
    else:
        missing.append(old.splitlines()[0][:64])

p.write_text(src, encoding="utf-8")
print(f"applied {applied}/{len(edits)} edits")
for m in missing:
    print(f"  NOT FOUND: {m}")
sys.exit(1 if missing else 0)
