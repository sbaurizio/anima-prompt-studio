"""Run by the WebUI at startup (before the UI is built).

On Windows it installs onnxruntime-directml into this extension's own
`vendor/` folder, so the tagger can use any DirectX 12 GPU (AMD, NVIDIA,
Intel). The WebUI virtual environment is never modified.

On other systems nothing is installed: the tagger uses the onnxruntime already
present in the WebUI environment (CPU, or CUDA with onnxruntime-gpu).
"""
import os
import pathlib
import subprocess
import sys

EXT_DIR = pathlib.Path(__file__).resolve().parent
VENDOR_DIR = EXT_DIR / "vendor"
PACKAGE = "onnxruntime-directml"


def _vendored() -> bool:
    # the dist-info folder is written last: without it the install was interrupted
    return (VENDOR_DIR / "onnxruntime" / "__init__.py").exists() and any(
        VENDOR_DIR.glob("onnxruntime_directml-*.dist-info"))


def main() -> None:
    if os.name != "nt" or os.environ.get("ANIMA_SKIP_VENDOR"):
        return
    if _vendored():
        return
    print(f"[Anima Prompt Studio] installing {PACKAGE} into {VENDOR_DIR.name}/ (one time only)...")
    VENDOR_DIR.mkdir(exist_ok=True)
    # --no-deps: numpy, protobuf etc. come from the WebUI environment; copying
    # them into vendor/ would shadow the WebUI's own versions
    cmd = [sys.executable, "-m", "pip", "install", "--no-deps", "--upgrade",
           "--target", str(VENDOR_DIR), PACKAGE]
    try:
        subprocess.run(cmd, check=True)
    except Exception as exc:
        print(f"[Anima Prompt Studio] could not install {PACKAGE}: {exc}. "
              "The tagger will fall back to the WebUI's onnxruntime (CPU).")


main()
