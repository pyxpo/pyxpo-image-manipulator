"""On-device image processing: info, crop/resize/convert/strip, and "make it under N bytes".

Native side exposes four primitives (`info`, `open`, `render`, `close`) on a background queue; everything else,
including the size search, is plain Python so both platforms share it.
"""
from __future__ import annotations

import math
import os
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, Union

from pyxpo.module_api import call as _call, require_linked, snake_aliases

__version__ = "0.1.0"

Info = Dict[str, Any]
Crop = Union[None, str, Dict[str, float]]
_MAX_EDGE = 4096  # decode cap for size search (memory safety)


class ImageTooLargeError(RuntimeError):
    """The target size cannot be reached without going below `minEdge` pixels."""


def call(method: str, *args: Any) -> Any:
    require_linked("ImageManipulator", "image-manipulator", package="pyxpo-image-manipulator")
    return _call("ImageManipulator", method, *args, reply=True)


def parse_ratio(text: str) -> float:
    try:
        w, h = text.split(":")
        r = float(w) / float(h)
    except (ValueError, ZeroDivisionError):
        raise ValueError("bad crop ratio %r (use 'W:H', e.g. '4:5')" % text)
    if r <= 0:
        raise ValueError("bad crop ratio %r" % text)
    return r


def centered_crop(width: int, height: int, ratio: float) -> Dict[str, int]:
    """Largest centred rectangle with width/height == ratio inside width x height."""
    if width / height > ratio:
        w, h = int(round(height * ratio)), height
    else:
        w, h = width, int(round(width / ratio))
    return {"x": (width - w) // 2, "y": (height - h) // 2, "width": w, "height": h}


def fit_size(width: int, height: int, want: Dict[str, Any]) -> Tuple[int, int]:
    """Target (w, h) for `resize={"width","height","mode"}`: "exact" uses both; "fit" keeps aspect inside the box."""
    rw, rh = want.get("width"), want.get("height")
    if not rw and not rh:
        raise ValueError("resize needs width and/or height")
    if rw and rh and want.get("mode", "exact") == "exact":
        return int(rw), int(rh)
    scale = min(float(rw) / width if rw else math.inf, float(rh) / height if rh else math.inf)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


async def search_under(render: Callable[[float, float], Awaitable[Info]], max_bytes: int, longest: int, min_edge: int,
                       lossy: bool = True, quality_hi: float = 0.92, quality_lo: float = 0.30) -> Info:
    """Finds a render with fileSize <= max_bytes: highest quality that fits, shrinking the scale when none does.
    `render(quality, scale)` returns an info dict whose `uri` is a fresh file. Losing files are deleted."""
    scale = 1.0
    for _ in range(5):
        best: Optional[Info] = None
        last: Optional[Info] = None
        if lossy:
            lo, hi = quality_lo, quality_hi
            first = await render(quality_hi, scale)
            if first["fileSize"] <= max_bytes:
                return first
            last = first
            for _i in range(6):
                q = (lo + hi) / 2
                out = await render(q, scale)
                if out["fileSize"] <= max_bytes:
                    if best is not None:
                        _discard(best)
                    best, lo = out, q
                else:
                    _discard(out)
                    hi = q
            _discard(first)
            if best is not None:
                best["quality"], best["scale"] = lo, scale
                return best
            last = await render(quality_lo, scale)
            _discard(last)  # only its size matters for the next scale
        else:
            last = await render(1.0, scale)
            if last["fileSize"] <= max_bytes:
                return last
            _discard(last)
        shrink = min(0.95, max(0.5, math.sqrt(max_bytes / float(last["fileSize"])) * 0.95))
        scale *= shrink
        if longest * scale < min_edge:
            break
    raise ImageTooLargeError("cannot reach %d bytes without going below %d px" % (max_bytes, min_edge))


def _discard(info: Info) -> None:
    try:
        os.remove(info["uri"])
    except OSError:
        pass


@snake_aliases
class ImageManipulator:
    @staticmethod
    async def getInfo(uri: str) -> Info:
        """`{uri, width, height, fileSize, format, mimeType, hasExif, hasGps}` (width/height after EXIF orientation)."""
        return await call("info", str(uri))

    @staticmethod
    async def process(uri: str, crop: Crop = None, resize: Optional[Dict[str, Any]] = None, format: str = "same",
                      quality: float = 0.9, maxEdge: Optional[int] = None) -> Info:
        """Orientation baked in, then crop, resize, encode. The output never carries EXIF/GPS.
        `format`: "same" | "jpeg" | "png" | "heic" (heic encode is iOS only)."""
        h = await call("open", str(uri), int(maxEdge or 0))
        try:
            w, ht = h["width"], h["height"]
            spec: Dict[str, Any] = {"format": format, "quality": float(quality)}
            if isinstance(crop, str):
                spec["crop"] = centered_crop(w, ht, parse_ratio(crop))
            elif crop:
                k = w / float(h.get("sourceWidth") or w)
                spec["crop"] = {n: int(round(crop[n] * k)) for n in ("x", "y", "width", "height")}
            if spec.get("crop"):
                w, ht = spec["crop"]["width"], spec["crop"]["height"]
            if resize:
                spec["width"], spec["height"] = fit_size(w, ht, resize)
            return await call("render", h["h"], spec)
        finally:
            await call("close", h["h"])

    @staticmethod
    async def compressToSize(uri: str, maxBytes: int, format: str = "jpeg", minEdge: int = 320) -> Info:
        """Re-encodes `uri` to at most `maxBytes`. Result has `quality` and `scale` when the search ran.
        Raises ImageTooLargeError when the target needs fewer than `minEdge` pixels on the long side."""
        if maxBytes <= 0:
            raise ValueError("maxBytes must be positive")
        h = await call("open", str(uri), _MAX_EDGE)
        try:
            w, ht = h["width"], h["height"]

            async def render(q: float, scale: float) -> Info:
                spec: Dict[str, Any] = {"format": format, "quality": q}
                if scale < 1.0:
                    spec["width"], spec["height"] = max(1, int(w * scale)), max(1, int(ht * scale))
                return await call("render", h["h"], spec)

            return await search_under(render, int(maxBytes), max(w, ht), int(minEdge), lossy=format != "png")
        finally:
            await call("close", h["h"])

    @staticmethod
    async def optimize(uri: str) -> Info:
        """Quick Optimize: long edge <= 2560 px, JPEG quality 0.8."""
        return await ImageManipulator.process(uri, format="jpeg", quality=0.8, maxEdge=2560)
