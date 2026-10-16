"""Desktop backend for `pyxpo run desktop` (Pillow; HEIC through macOS `sips`). Same primitives as the native side."""
from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageOps = None  # type: ignore

_FORMATS = {"JPEG": "jpeg", "PNG": "png", "HEIF": "heic", "GIF": "gif", "WEBP": "webp"}
_MIME = {"jpeg": "image/jpeg", "png": "image/png", "heic": "image/heic", "gif": "image/gif", "webp": "image/webp"}
_lock = threading.Lock()
_ids = itertools.count(1)
_open: Dict[int, Tuple[Any, str]] = {}


def _path(uri: str) -> str:
    return uri[7:] if uri.startswith("file://") else uri


def _out_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pyxpo" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(path: str) -> Tuple[Any, str]:
    """Decoded image (EXIF orientation applied) and its format name."""
    if Image is None:
        raise RuntimeError("image-manipulator on desktop needs Pillow: pip install Pillow")
    try:
        img = Image.open(path)
        fmt = _FORMATS.get(img.format or "", "other")
    except Exception:  # noqa: BLE001  HEIC and others Pillow cannot read: let sips convert
        if not shutil.which("sips"):
            raise RuntimeError("cannot read image %s" % path)
        tmp = os.path.join(tempfile.mkdtemp(prefix="pyxpo-"), "in.png")
        subprocess.run(["sips", "-s", "format", "png", path, "--out", tmp], capture_output=True, check=True)
        img = Image.open(tmp)
        fmt = "heic" if path.lower().endswith((".heic", ".heif")) else "other"
    img.load()
    return img, fmt


def info(uri: str) -> Dict[str, Any]:
    path = _path(uri)
    img, fmt = _load(path)
    exif = img.getexif() if hasattr(img, "getexif") else {}
    w, h = img.size
    if exif.get(0x0112) in (5, 6, 7, 8):
        w, h = h, w
    return {"uri": path, "width": w, "height": h, "fileSize": os.path.getsize(path), "format": fmt,
            "mimeType": _MIME.get(fmt, "application/octet-stream"), "hasExif": len(exif) > 0,
            "hasGps": 0x8825 in exif}


def open_(uri: str, max_edge: int) -> Dict[str, Any]:
    img, fmt = _load(_path(uri))
    img = ImageOps.exif_transpose(img)
    sw, sh = img.size
    if max_edge and max(sw, sh) > max_edge:
        s = max_edge / float(max(sw, sh))
        img = img.resize((max(1, round(sw * s)), max(1, round(sh * s))), Image.LANCZOS)
    with _lock:
        h = next(_ids)
        _open[h] = (img, fmt)
    return {"h": h, "width": img.size[0], "height": img.size[1], "sourceWidth": sw, "sourceHeight": sh}


def _apply(img: Any, op: Dict[str, Any]) -> Any:
    kind = op.get("op")
    if kind == "crop":
        x, y, w, h = int(op["x"]), int(op["y"]), int(op["width"]), int(op["height"])
        x0, y0, x1, y1 = max(0, x), max(0, y), min(img.size[0], x + w), min(img.size[1], y + h)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("crop outside image")
        return img.crop((x0, y0, x1, y1))
    if kind == "resize":
        return img.resize((int(op["width"]), int(op["height"])), Image.LANCZOS)
    if kind == "rotate":  # clockwise, multiples of 90
        d = int(op.get("degrees", 0)) % 360
        if d % 90:
            raise ValueError("rotate supports multiples of 90 degrees")
        return {0: img, 90: img.transpose(Image.ROTATE_270), 180: img.transpose(Image.ROTATE_180),
                270: img.transpose(Image.ROTATE_90)}[d]
    if kind == "flip":
        return img.transpose(Image.FLIP_TOP_BOTTOM if op.get("axis") == "vertical" else Image.FLIP_LEFT_RIGHT)
    raise ValueError("unknown action")


def render(h: int, spec: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        if h not in _open:
            raise ValueError("bad image handle")
        img, src_fmt = _open[h]
    for op in spec.get("ops") or []:
        img = _apply(img, op)
    c = spec.get("crop")
    if c:
        img = _apply(img, dict(c, op="crop"))
    fmt = spec.get("format") or "same"
    if fmt == "same":
        fmt = src_fmt if src_fmt in ("jpeg", "png", "heic") else "jpeg"
    if fmt not in ("jpeg", "png", "heic"):
        raise ValueError("unsupported output format %s" % fmt)
    tw, th = int(spec.get("width") or img.size[0]), int(spec.get("height") or img.size[1])
    if not (0 < tw <= 16384 and 0 < th <= 16384):
        raise ValueError("bad output size %dx%d" % (tw, th))
    if (tw, th) != img.size:
        img = img.resize((tw, th), Image.LANCZOS)
    quality = max(1, min(100, int(round(float(spec.get("quality", 0.9)) * 100))))
    out = _out_dir() / (uuid.uuid4().hex + "." + ("jpg" if fmt == "jpeg" else fmt))
    if fmt == "png":
        img.save(out, "PNG")
    else:
        if img.mode in ("RGBA", "LA", "P"):  # JPEG/HEIC have no alpha: flatten onto white
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if fmt == "jpeg":
            img.save(out, "JPEG", quality=quality)
        else:
            if not shutil.which("sips"):
                raise ValueError("HEIC output needs macOS")
            tmp = out.with_suffix(".png")
            img.save(tmp, "PNG")
            try:
                subprocess.run(["sips", "-s", "format", "heic", "-s", "formatOptions", str(quality), str(tmp),
                                "--out", str(out)], capture_output=True, check=True)
            finally:
                tmp.unlink()
    return {"uri": str(out), "fileSize": out.stat().st_size, "width": tw, "height": th, "format": fmt,
            "mimeType": _MIME[fmt]}


def close(h: int) -> None:
    with _lock:
        _open.pop(h, None)


def call(method: str, args: List[Any]) -> Any:
    fn = {"info": info, "open": open_, "render": render, "close": close}.get(method)
    if fn is None:
        raise ValueError("unknown ImageManipulator method %r" % method)
    return fn(*args)
