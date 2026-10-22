"""WD v3 tagger (ONNX), GPU-accelerated through DirectML or CUDA when available.

Extracts general tags, character tags and the rating from an image.
"""
from __future__ import annotations

import csv
import gc
import pathlib
import threading
import time
from typing import Any, Iterator

import numpy as np
import requests
from PIL import Image

try:
    import onnxruntime as ort
except Exception as exc:  # pragma: no cover
    ort = None  # type: ignore
    _ORT_ERR = exc

KAOMOJI = {
    "0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>", "=_=", ">_<",
    "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u", "x_x", "|_|", "||_||",
}

# categories in the WD v3 selected_tags.csv
CAT_GENERAL = 0
CAT_CHARACTER = 4
CAT_RATING = 9

# WD v3 taggers published by SmilingWolf on Hugging Face
KNOWN_TAGGERS = [
    "SmilingWolf/wd-eva02-large-tagger-v3",
    "SmilingWolf/wd-vit-large-tagger-v3",
    "SmilingWolf/wd-swinv2-tagger-v3",
    "SmilingWolf/wd-convnext-tagger-v3",
    "SmilingWolf/wd-vit-tagger-v3",
]
MODEL_FILES = ("selected_tags.csv", "model.onnx")


class TaggerError(RuntimeError):
    pass


def model_present(model_dir: str | pathlib.Path) -> bool:
    d = pathlib.Path(model_dir)
    return all((d / f).exists() for f in MODEL_FILES)


def download_model(repo: str, dest: str | pathlib.Path) -> Iterator[str]:
    """Download model.onnx + selected_tags.csv from Hugging Face.

    Yields human-readable progress lines. Only the two model files are
    requested; nothing about the user is sent.
    """
    dest = pathlib.Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        url = f"https://huggingface.co/{repo}/resolve/main/{name}"
        part = dest / (name + ".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            last = 0.0
            with open(part, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last > 1.0:
                        last = now
                        pct = f" ({done * 100 // total}%)" if total else ""
                        yield f"{name}: {done / 1e6:.0f} MB{pct}"
        part.replace(dest / name)
        yield f"{name}: done"


def _providers(use_gpu: bool, device_id: int) -> list[Any]:
    available = ort.get_available_providers()
    out: list[Any] = []
    if use_gpu:
        if "CUDAExecutionProvider" in available:
            out.append(("CUDAExecutionProvider", {"device_id": int(device_id)}))
        if "DmlExecutionProvider" in available:
            out.append(("DmlExecutionProvider", {"device_id": int(device_id)}))
    out.append("CPUExecutionProvider")
    return out


class WDTagger:
    """Wrapper around the ONNX model. Loading is lazy and thread-safe."""

    def __init__(
        self,
        model_dir: str | pathlib.Path,
        *,
        use_gpu: bool = True,
        device_id: int = 0,
    ):
        self.model_dir = pathlib.Path(model_dir)
        self.model_path = self.model_dir / "model.onnx"
        self.csv_path = self.model_dir / "selected_tags.csv"
        self.use_gpu = use_gpu
        self.device_id = device_id
        self.target_size = 448

        self._session = None
        self._names: list[str] = []
        self._cats: list[int] = []
        self._lock = threading.Lock()
        self.active_provider = "not loaded"
        self.load_seconds = 0.0

    # ---------------------------------------------------------------- loading
    @property
    def is_loaded(self) -> bool:
        return self._session is not None

    def _read_tags(self) -> None:
        names: list[str] = []
        cats: list[int] = []
        with open(self.csv_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                n = row["name"]
                names.append(n if n in KAOMOJI else n.replace("_", " "))
                cats.append(int(row["category"]))
        self._names, self._cats = names, cats

    def load(self) -> str:
        """Load the model. Returns a string describing the device in use."""
        with self._lock:
            if self._session is not None:
                return self.active_provider
            if ort is None:  # pragma: no cover
                raise TaggerError(f"onnxruntime not available: {_ORT_ERR}")
            if not model_present(self.model_dir):
                raise TaggerError(
                    f"Tagger model not found in {self.model_dir}. "
                    "Download it from Settings → Tagger."
                )

            self._read_tags()

            t0 = time.perf_counter()
            try:
                self._session = ort.InferenceSession(
                    str(self.model_path),
                    providers=_providers(self.use_gpu, self.device_id),
                )
            except Exception as exc:
                # GPU providers can fail (driver, VRAM): fall back to CPU
                self._session = ort.InferenceSession(
                    str(self.model_path), providers=["CPUExecutionProvider"]
                )
                self.active_provider = f"CPU (GPU failed: {exc})"
                self.load_seconds = time.perf_counter() - t0
                self._read_input_size()
                return self.active_provider

            used = self._session.get_providers()[0]
            if "Dml" in used:
                self.active_provider = f"GPU DirectML (device {self.device_id})"
            elif "CUDA" in used:
                self.active_provider = f"GPU CUDA (device {self.device_id})"
            else:
                self.active_provider = "CPU"
            self.load_seconds = time.perf_counter() - t0
            self._read_input_size()
            return self.active_provider

    def _read_input_size(self) -> None:
        # WD v3 input is NHWC: [batch, height, width, 3]
        try:
            shape = self._session.get_inputs()[0].shape
            if isinstance(shape[1], int):
                self.target_size = int(shape[1])
        except Exception:
            pass

    def unload(self) -> None:
        with self._lock:
            self._session = None
        gc.collect()

    # ------------------------------------------------------------ preprocessing
    def preprocess(self, image: Image.Image) -> np.ndarray:
        """Standard WD pipeline: white background, square padding, BGR."""
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, (255, 255, 255))
        canvas.alpha_composite(image)
        rgb = canvas.convert("RGB")

        w, h = rgb.size
        m = max(w, h)
        padded = Image.new("RGB", (m, m), (255, 255, 255))
        padded.paste(rgb, ((m - w) // 2, (m - h) // 2))
        if m != self.target_size:
            padded = padded.resize((self.target_size, self.target_size), Image.BICUBIC)

        arr = np.asarray(padded, dtype=np.float32)[:, :, ::-1]  # RGB -> BGR
        return np.expand_dims(arr, axis=0)

    # -------------------------------------------------------------- inference
    def predict_raw(self, image: Image.Image) -> np.ndarray:
        self.load()
        arr = self.preprocess(image)
        input_name = self._session.get_inputs()[0].name
        outputs = self._session.run(None, {input_name: arr})
        return outputs[0][0]

    def predict(
        self,
        image: Image.Image,
        *,
        general_threshold: float = 0.35,
        character_threshold: float = 0.85,
        rating_threshold: float = 0.30,
    ) -> dict[str, Any]:
        """Return general/character tags sorted by confidence, plus the rating."""
        preds = self.predict_raw(image)
        names, cats = self._names, self._cats

        general: list[tuple[str, float]] = []
        character: list[tuple[str, float]] = []
        rating: list[tuple[str, float]] = []

        for i, score in enumerate(preds):
            if i >= len(names):
                break
            cat = cats[i]
            s = float(score)
            if cat == CAT_GENERAL:
                if s >= general_threshold:
                    general.append((names[i], s))
            elif cat == CAT_CHARACTER:
                if s >= character_threshold:
                    character.append((names[i], s))
            elif cat == CAT_RATING:
                if s >= rating_threshold:
                    rating.append((names[i], s))

        general.sort(key=lambda x: -x[1])
        character.sort(key=lambda x: -x[1])
        rating.sort(key=lambda x: -x[1])

        return {
            "general": general,
            "character": character,
            "rating": rating[0][0] if rating else "",
            "rating_scores": rating,
            "provider": self.active_provider,
        }


class TaggerService:
    """Shared service: loads the model in the background."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.tagger: WDTagger | None = None
        self.ready = threading.Event()
        self.error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._load, daemon=True)
        self._thread.start()

    def _load(self) -> None:
        try:
            self.tagger = WDTagger(
                self.cfg.tagger_dir(),
                use_gpu=bool(self.cfg.get("tagger", "use_gpu", default=True)),
                device_id=int(self.cfg.get("tagger", "device_id", default=0)),
            )
            self.tagger.load()
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.ready.set()

    def predict(self, image: Image.Image, **kw) -> dict[str, Any]:
        if not self.tagger:
            raise TaggerError(self.error or "Tagger not initialized")
        return self.tagger.predict(image, **kw)

    def unload(self) -> None:
        if self.tagger:
            self.tagger.unload()
