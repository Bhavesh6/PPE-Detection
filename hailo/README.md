# Running the PPE model on the Pi's Hailo AI HAT

What the accelerator gives us, what state it's in, and the exact steps to get
our own model onto it.

## Where things stand

Verified on the Pi (2026-08-18), not assumed:

| | |
|---|---|
| HAT | Connected, PCIe Gen 3, driver `hailo` loaded, `/dev/hailo0` present |
| Architecture | **HAILO8L** (the board name string says "Hailo-8" — ignore it) |
| Firmware / runtime | 4.17.0 |
| Measured speed | **87.1 FPS, 9.96 ms** (YOLOv8n @ 640x640, on this chip) |
| Our model as HEF | **Not compiled yet** — this is the remaining work |

Our model is YOLOv8n at 640x640 with 10 classes, so once compiled it should
land in the same 87 FPS range — slightly faster, since it has 10 classes
instead of the 80 in the benchmarked model. The accelerator is not the
bottleneck: the checkpoint app throttles inference to one frame every 0.5s.

## Why this can't be done on the Pi

The Hailo Dataflow Compiler (DFC) — the tool that turns ONNX into HEF — is
published for **x86_64 Linux only**. There is no ARM build, so it cannot run
on the Pi regardless of how much is installed there. The Pi runs the
*result*; a separate x86 machine produces it.

---

## Step 1 — Get an x86_64 Linux environment

On the Windows PC (it is AMD64, which is correct). Open PowerShell **as
Administrator** — this needs elevation:

```powershell
wsl --install -d Ubuntu
```

Reboot if prompted, then set a username/password when Ubuntu first launches.

> This machine has the **legacy inbox WSL** (`wsl --version` is not a
> recognised argument), whose catalogue only offers a generic `Ubuntu` —
> `wsl --install -d Ubuntu-22.04` fails with "Invalid distribution name".
> Install the generic one and check what you got with `lsb_release -a`.

**The compiler needs Python 3.8–3.10.** If the distro turns out to be Ubuntu
24.04 (Python 3.12), don't reinstall — add 3.10 alongside it:

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3.10-dev
```

Ubuntu 22.04 already ships Python 3.10 and needs none of this.

## Step 2 — Register and download the compiler

1. Create a free account at <https://hailo.ai/developer-zone/>
2. Go to **Software Downloads → Dataflow Compiler**
3. Download the Linux x86_64 wheel for Python 3.10, e.g.
   `hailo_dataflow_compiler-<version>-py3-none-linux_x86_64.whl`

> **Version compatibility — read this before downloading.**
> A HEF carries a format version tied to the compiler that produced it, and a
> HEF built by a much newer DFC may refuse to load on our **HailoRT 4.17.0**
> runtime. Prefer the DFC release whose notes name HailoRT 4.17. If you can
> only get a newer DFC, plan to upgrade HailoRT on the Pi to match — but note
> the Pi is shared, so coordinate that with whoever else uses it.

## Step 3 — Install it

Inside the Ubuntu 22.04 shell:

```bash
sudo apt update && sudo apt install -y python3.10-venv python3-pip
python3.10 -m venv ~/hailo_env
source ~/hailo_env/bin/activate
pip install --upgrade pip
pip install /mnt/c/Users/<you>/Downloads/hailo_dataflow_compiler-*.whl
python -c "import hailo_sdk_client; print('DFC ready')"
```

Your Windows drives appear under `/mnt/c/`, so no file copying is needed.

## Step 4 — Collect the inputs

You need two things in one working directory:

- **`best.onnx`** — already exported and verified. Input `images[1,3,640,640]`,
  output `[1,14,8400]` (4 box + 10 classes), opset 11.
- **Calibration images** — real frames, *not* random data (see below).

```bash
mkdir -p ~/ppe && cd ~/ppe
cp /mnt/c/Users/<you>/Downloads/construction-ppe-detection-main/construction-ppe-detection-main/best.onnx .
mkdir calib_images
cp /mnt/d/PPE-Detection/backend/instance/evidence/*.jpg calib_images/
```

That gives 18 real gate frames. It will work, but it is thin — Hailo suggests
64+, ideally several hundred, spanning the lighting, distances and gear the
gate actually sees. Capturing a few hundred frames from the Pi's own camera
is the single cheapest thing you can do for accuracy.

## Step 5 — Compile

```bash
cp /mnt/d/PPE-Detection/hailo/compile_ppe_hef.py .
python compile_ppe_hef.py --onnx best.onnx --hef best.hef --arch hailo8l --calib-dir ./calib_images
```

Expect this to take a while and to use several GB of RAM.

## Step 6 — Put it on the Pi and verify

```bash
scp best.hef nexus@<pi-address>:~/safetyfirst-checkpoint/
```

Then on the Pi:

```bash
hailortcli parse-hef ~/safetyfirst-checkpoint/best.hef
hailortcli benchmark --no-power true ~/safetyfirst-checkpoint/best.hef
```

`parse-hef` must report `Architecture HEF was compiled for: HAILO8L`. The
`--no-power` flag is required — this board does not support power measurement
and the benchmark fails without it.

---

## Don't use the earlier `compile_hailo.py`

The script that came with the teammate's folder has three defects, each of
which produces a failed compile or a quietly bad model:

1. **Defaults to `hailo8`.** Our board is HAILO8L; the HEF will not load.
2. **No `end_node_names`.** YOLOv8's ONNX ends in the DFL decode head —
   Concat/Sub/Split ops the accelerator cannot map. The compile has to be cut
   at the six head convolutions (listed in `compile_ppe_hef.py`, read off our
   actual graph).
3. **Calibrates on `np.random.uniform` noise.** INT8 quantisation derives its
   activation ranges from the calibration set. Noise gives ranges unrelated to
   real frames and accuracy drops with no error reported.

`compile_ppe_hef.py` in this directory fixes all three. It has not been run
end to end — no DFC was available to test it — so treat it as reviewed code,
not verified code.

## After it works

Getting a HEF is not the same as using it. The checkpoint app currently sends
frames to the **backend** for inference; switching to on-HAT inference is an
architectural change, not a drop-in swap. It also needs the `hailo_platform`
Python bindings, which are being built from source on the Pi (the public wheel
host now 404s on every version, which is why the wheels in
`~/hailo-rpi5-examples/hailo_temp_resources/` are 0 bytes).
