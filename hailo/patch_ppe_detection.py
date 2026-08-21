"""Point ppe_detection.py at the exported model folder instead of guesses.

Safe to re-run: an edit already present is reported as such rather than
applied twice.

The argparse edits are regular expressions, not literals, because the
source has trailing spaces after some commas ("default=0.45, \\n") that
are invisible in an editor and in terminal output. Matching those exactly
fails in a way that looks like the line is missing entirely.
"""

import pathlib
import re
import shutil
import sys

TARGET = pathlib.Path.home() / "Downloads/construction-ppe-detection-main/ppe_detection.py"

IMPORT_BLOCK = (
    "from model_config import load_model_config\n\n"
    "# Set from the model at startup; extract_detections() needs it to undo\n"
    "# the letterbox, and hardcoding 640 breaks silently on any other export.\n"
    "MODEL_INPUT = [640, 640]        # [height, width]\n\n"
    "# Fallback only — the real list comes from metadata.yaml."
)

USE_MODEL = (
    "    model = load_model_config(hef_override=args.hef)\n"
    "    print(model.describe())\n"
    "    args.hef = model.hef\n"
    "    # An explicit --labels still wins; everything else defers to the\n"
    "    # export, so the script cannot drift from the model it loads.\n"
    "    classes = load_classes(args.labels) if args.labels else model.classes\n"
    "    if args.conf_thresh is None:\n"
    "        args.conf_thresh = model.conf_thresh\n"
    "    if args.iou_thresh is None:\n"
    "        args.iou_thresh = model.iou_thresh\n"
    "    MODEL_INPUT[0], MODEL_INPUT[1] = model.imgsz[0], model.imgsz[1]"
)

# (name, pattern, replacement, already_present_marker)
EDITS = [
    ("hef default",
     r'default="best\.hef",\s*\n(\s*)help="[^"]*"',
     'default=None,\n\\1help="Path to a .hef (default: best/best_hailo_model/best.hef)"',
     'best/best_hailo_model/best.hef'),

    ("conf threshold default",
     r'default=0\.45,\s*\n(\s*)help="Confidence threshold[^"]*"',
     'default=None,\n\\1help="Confidence threshold (default: from the model\'s nms_config.json)"',
     "Confidence threshold (default: from the model"),

    ("iou threshold default",
     r'default=0\.45,\s*\n(\s*)help="NMS IoU threshold[^"]*"',
     'default=None,\n\\1help="NMS IoU threshold (default: from the model\'s nms_config.json)"',
     "NMS IoU threshold (default: from the model"),

    ("import + MODEL_INPUT",
     re.escape("# Default PPE class names (modify to match your model's classes)"),
     IMPORT_BLOCK.replace("\\", "\\\\"),
     "from model_config import load_model_config"),

    ("use the model's own answers",
     re.escape('    classes = load_classes(args.labels)\n'
               '    print(f"[INFO] Loaded {len(classes)} classes: {classes}")'),
     USE_MODEL.replace("\\", "\\\\"),
     "load_model_config(hef_override=args.hef)"),

    ("letterbox x1", re.escape("xmin * 640"), "xmin * MODEL_INPUT[1]", "xmin * MODEL_INPUT[1]"),
    ("letterbox y1", re.escape("ymin * 640"), "ymin * MODEL_INPUT[0]", "ymin * MODEL_INPUT[0]"),
    ("letterbox x2", re.escape("xmax * 640"), "xmax * MODEL_INPUT[1]", "xmax * MODEL_INPUT[1]"),
    ("letterbox y2", re.escape("ymax * 640"), "ymax * MODEL_INPUT[0]", "ymax * MODEL_INPUT[0]"),

    # The model says lowercase; the colour table keyed on the old names
    # would never have matched these two.
    ("lowercase machinery/vehicle",
     r'    "Machinery",\s*\n    "Vehicle"',
     '    "machinery",\n    "vehicle"',
     '"machinery",'),
]


def main() -> int:
    if not TARGET.is_file():
        print(f"[ERROR] {TARGET} not found")
        return 1

    src = TARGET.read_text(encoding="utf-8")
    original = src
    applied, already, missing = [], [], []

    for name, pattern, replacement, marker in EDITS:
        if marker in src:
            already.append(name)
            continue
        new_src, count = re.subn(pattern, replacement, src, count=1)
        if count:
            src = new_src
            applied.append(name)
        else:
            missing.append(name)

    if src != original:
        # One backup per run, kept only if there wasn't one already: a
        # second run would otherwise overwrite the pristine original with
        # a half-patched copy.
        backup = TARGET.with_suffix(".py.orig")
        if not backup.exists():
            shutil.copy(TARGET, backup)
            print(f"[backup] {backup}")
        TARGET.write_text(src, encoding="utf-8")

    for n in applied:
        print(f"  applied      {n}")
    for n in already:
        print(f"  already done {n}")
    for n in missing:
        print(f"  NOT FOUND    {n}")

    print(f"\n{len(applied)} applied, {len(already)} already in place, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
