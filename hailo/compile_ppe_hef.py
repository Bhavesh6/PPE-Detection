"""Compile the PPE YOLOv8n model (best.onnx) to a Hailo HEF.

Runs where the Hailo Dataflow Compiler lives — x86_64 Linux (or WSL2), NOT on
the Pi: the DFC has no ARM build. The resulting .hef is then copied to the Pi.

This is a corrected version of the earlier `compile_hailo.py`. Three things
were wrong there, each of which produces either a failed compile or a model
that silently detects badly:

1. It defaulted to ``hailo8``. The board on our Pi reports its architecture as
   **HAILO8L** (confirmed via ``hailortcli fw-control identify``), and a hailo8
   HEF will not load on it. Default here is hailo8l.

2. It passed no ``end_node_names``. A YOLOv8 ONNX export ends with the DFL
   decode head — Concat/Sub/Add/Split reshaping ops that the compiler cannot
   map to the accelerator. The compile is cut at the six head convolutions
   instead, and the decode runs on the host (which is what the Pi app already
   does with the raw tensors).

3. It calibrated on ``np.random.uniform`` noise. Quantisation to INT8 derives
   its activation ranges from the calibration set, so noise yields ranges that
   have nothing to do with real frames, and accuracy degrades in a way no
   error message reports. Real images are required.

Usage:
    python compile_ppe_hef.py --onnx best.onnx --calib-dir ./calib_images
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# The six convolutions that terminate YOLOv8's detect head — three scales,
# each with a box branch (cv2) and a class branch (cv3). Read off the actual
# graph of our best.onnx, not copied from a generic example.
YOLOV8_END_NODES = [
    "/model.22/cv2.0/cv2.0.2/Conv",
    "/model.22/cv3.0/cv3.0.2/Conv",
    "/model.22/cv2.1/cv2.1.2/Conv",
    "/model.22/cv3.1/cv3.1.2/Conv",
    "/model.22/cv2.2/cv2.2.2/Conv",
    "/model.22/cv3.2/cv3.2.2/Conv",
]

IMG_SIZE = 640


def load_calibration(calib_dir: str, limit: int) -> np.ndarray:
    """Load real frames as NHWC float32 in 0-255.

    Hailo applies input normalisation on-chip when the model script declares a
    ``normalization`` layer, so the calibration set is fed in the raw 0-255
    range it will see at runtime. If you instead bake normalisation into the
    ONNX, feed 0-1 here to match — the two must agree or every activation
    range is wrong.
    """
    try:
        from PIL import Image
    except ImportError:
        sys.exit("Pillow is required to load calibration images: pip install pillow")

    exts = (".jpg", ".jpeg", ".png", ".bmp")
    paths = sorted(
        os.path.join(calib_dir, f)
        for f in os.listdir(calib_dir)
        if f.lower().endswith(exts)
    )[:limit]

    if not paths:
        sys.exit(f"No images found in {calib_dir!r} — refusing to calibrate on noise.")

    frames = [
        np.asarray(
            Image.open(p).convert("RGB").resize((IMG_SIZE, IMG_SIZE)),
            dtype=np.float32,
        )
        for p in paths
    ]
    data = np.stack(frames)
    print(f"[calib] {len(paths)} real images -> {data.shape}")
    if len(paths) < 64:
        print(
            f"[calib] WARNING: {len(paths)} images is thin. Hailo suggests 64+ "
            "(ideally several hundred) covering the lighting, distance and gear "
            "the gate actually sees. Expect some accuracy loss."
        )
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Compile PPE YOLOv8 ONNX to Hailo HEF")
    ap.add_argument("--onnx", default="best.onnx")
    ap.add_argument("--hef", default="best.hef")
    ap.add_argument(
        "--arch",
        default="hailo8l",
        choices=["hailo8", "hailo8l", "hailo15h"],
        help="Our Pi's HAT reports HAILO8L — the default. Do not change without checking.",
    )
    ap.add_argument("--calib-dir", required=True, help="Directory of real frames")
    ap.add_argument("--calib-count", type=int, default=256)
    ap.add_argument("--alls", default="", help="Optional Hailo model script (.alls)")
    args = ap.parse_args()

    try:
        from hailo_sdk_client import ClientRunner
    except ImportError:
        sys.exit(
            "hailo_sdk_client not found. The Dataflow Compiler must be installed in\n"
            "this environment, and it only exists for x86_64 Linux — it cannot run on\n"
            "the Raspberry Pi. Get the wheel from the Hailo Developer Zone (free\n"
            "account) and install it into a Python 3.8-3.10 venv."
        )

    calib = load_calibration(args.calib_dir, args.calib_count)

    print(f"[1/4] Parsing {args.onnx} for {args.arch}")
    runner = ClientRunner(hw_arch=args.arch)
    runner.translate_onnx_model(
        args.onnx,
        "yolov8n_ppe",
        start_node_names=["images"],
        end_node_names=YOLOV8_END_NODES,
        net_input_shapes={"images": [1, 3, IMG_SIZE, IMG_SIZE]},
    )

    if args.alls and os.path.exists(args.alls):
        print(f"[2/4] Applying model script {args.alls}")
        runner.load_model_script(args.alls)
    else:
        print("[2/4] No .alls script — using compiler defaults")

    print(f"[3/4] Quantising on {len(calib)} real frames")
    runner.optimize(calib)

    print("[4/4] Compiling")
    hef = runner.compile()
    with open(args.hef, "wb") as fh:
        fh.write(hef)

    print(f"Wrote {args.hef} ({os.path.getsize(args.hef)} bytes) for {args.arch}")
    print("Verify on the Pi with:")
    print(f"  hailortcli parse-hef {args.hef}")
    print(f"  hailortcli benchmark --no-power true {args.hef}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
