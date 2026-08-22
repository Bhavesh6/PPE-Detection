"""Read a Hailo export folder and report what the model actually is.

Drop-in for ppe_detection.py. The exported folder carries everything the
script previously hardcoded:

    best/best_hailo_model/
        best.hef            the model
        metadata.yaml       class names, image size, architecture
        nms_config.json     score/IoU thresholds, class count

Hardcoding those is how a script drifts from its model. The class list in
particular was already wrong: it said "Machinery" and "Vehicle" while the
model says "machinery" and "vehicle", which silently breaks any lookup
keyed on the name — including the colour table.

No PyYAML dependency: metadata.yaml's `names:` block is a flat
"  <int>: <string>" mapping, and parsing that directly avoids adding an
install step to a Pi that already has enough of them.
"""

from __future__ import annotations

import json
import os
import re

# Only used if the folder has no metadata.yaml at all.
FALLBACK_CLASSES = [
    "Hardhat", "Mask", "NO-Hardhat", "NO-Mask", "NO-Safety Vest",
    "Person", "Safety Cone", "Safety Vest", "machinery", "vehicle",
]

DEFAULT_MODEL_DIR = os.path.join("best", "best_hailo_model")

_NAME_LINE = re.compile(r"^\s+(\d+)\s*:\s*(.+?)\s*$")


class ModelConfig:
    """What the exported folder says about this model."""

    def __init__(self, model_dir: str):
        self.dir = model_dir
        self.hef = os.path.join(model_dir, "best.hef")
        self.classes = list(FALLBACK_CLASSES)
        self.imgsz = (640, 640)
        self.conf_thresh = 0.25
        self.iou_thresh = 0.7
        self.arch = "unknown"
        self.nms_baked = False
        self.notes: list[str] = []

        self._read_metadata(os.path.join(model_dir, "metadata.yaml"))
        self._read_nms(os.path.join(model_dir, "nms_config.json"))

    # -- metadata.yaml ---------------------------------------------------
    def _read_metadata(self, path: str) -> None:
        if not os.path.isfile(path):
            self.notes.append(f"no metadata.yaml in {self.dir} — using built-in class list")
            return

        names: dict[int, str] = {}
        in_names = False
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip("\n")

                if stripped.startswith("names:"):
                    in_names = True
                    continue
                if in_names:
                    match = _NAME_LINE.match(stripped)
                    if match:
                        names[int(match.group(1))] = match.group(2).strip("'\"")
                        continue
                    # Any line that isn't indented "n: name" ends the block.
                    in_names = False

                if stripped.startswith("hailo_arch:"):
                    self.arch = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("nms:"):
                    self.nms_baked = stripped.split(":", 1)[1].strip().lower() == "true"

        if names:
            # Ordered by index, not by insertion: a class list in the wrong
            # order mislabels every detection while looking perfectly fine.
            self.classes = [names[i] for i in sorted(names)]

    # -- nms_config.json -------------------------------------------------
    def _read_nms(self, path: str) -> None:
        if not os.path.isfile(path):
            self.notes.append(f"no nms_config.json in {self.dir} — using default thresholds")
            return

        with open(path, encoding="utf-8") as handle:
            cfg = json.load(handle)

        self.conf_thresh = float(cfg.get("nms_scores_th", self.conf_thresh))
        self.iou_thresh = float(cfg.get("nms_iou_th", self.iou_thresh))

        dims = cfg.get("image_dims")
        if isinstance(dims, (list, tuple)) and len(dims) == 2:
            self.imgsz = (int(dims[0]), int(dims[1]))

        # The count in nms_config is authoritative for the compiled graph.
        # A mismatch against metadata means the two files came from
        # different exports, and every class id after the first extra one
        # would be shifted - worth refusing to guess about.
        declared = cfg.get("classes")
        if declared is not None and int(declared) != len(self.classes):
            self.notes.append(
                f"WARNING: nms_config says {declared} classes but metadata lists "
                f"{len(self.classes)} — these files may be from different exports"
            )

    def describe(self) -> str:
        lines = [
            f"[model] {self.hef}",
            f"[model] {len(self.classes)} classes: {', '.join(self.classes)}",
            f"[model] {self.imgsz[0]}x{self.imgsz[1]}, arch {self.arch}, "
            f"NMS {'baked into the HEF' if self.nms_baked else 'done in Python'}",
            f"[model] conf {self.conf_thresh}, IoU {self.iou_thresh}",
        ]
        lines += [f"[model] {n}" for n in self.notes]
        return "\n".join(lines)


def load_model_config(model_dir: str | None = None, hef_override: str | None = None) -> ModelConfig:
    """Resolve the model folder, preferring an explicit --hef if given."""
    if hef_override and os.path.isfile(hef_override):
        cfg = ModelConfig(os.path.dirname(hef_override) or ".")
        cfg.hef = hef_override
        return cfg
    return ModelConfig(model_dir or DEFAULT_MODEL_DIR)
