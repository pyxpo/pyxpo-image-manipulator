"""On-device image manipulation, shaped like `expo-image-manipulator`.

    from pyxpo_image_manipulator import ImageManipulator, SaveFormat, FlipType
    out = await ImageManipulator.manipulateAsync(uri, [{"resize": {"width": 800}}, {"rotate": 90}],
                                                 {"compress": 0.8, "format": SaveFormat.JPEG})

Native side exposes five primitives (`info`, `metadata`, `open`, `render`, `close`) on a background queue; everything else,
including the size search, is plain Python so both platforms share it. Output never carries EXIF/GPS.
Options that cannot work on the running platform raise `NotSupportedError` before any native call.
"""
from __future__ import annotations

import math
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import base64

from pyxpo.module_api import NotSupportedError, call as _call, require_linked, require_supported, snake_aliases

__version__ = "0.1.0a3"

Info = Dict[str, Any]
Crop = Union[None, str, Dict[str, float]]
_MAX_EDGE = 4096  # decode cap for size search (memory safety)


class SaveFormat:
    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    HEIC = "heic"  # iOS only, not in Expo


class FlipType:
    Vertical = "vertical"
    Horizontal = "horizontal"


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


def _plan(w: int, h: int, actions: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Turns Expo-style actions into native ops, tracking the image size so one-sided resizes keep the aspect."""
    ops: List[Dict[str, Any]] = []
    for a in actions:
        if not isinstance(a, dict) or len(a) != 1:
            raise ValueError("each action is a dict with one key: resize, rotate, flip or crop (got %r)" % (a,))
        (kind, arg), = a.items()
        if kind == "resize":
            rw, rh = arg.get("width"), arg.get("height")
            if not rw and not rh:
                raise ValueError("resize needs width and/or height")
            w, h = fit_size(w, h, dict(arg, mode="exact" if rw and rh else "fit"))
            ops.append({"op": "resize", "width": w, "height": h})
        elif kind == "rotate":
            deg = float(arg)
            if deg % 90:
                raise NotSupportedError("rotate supports multiples of 90 degrees only")
            deg = int(deg) % 360
            if deg:
                ops.append({"op": "rotate", "degrees": deg})
                if deg in (90, 270):
                    w, h = h, w
        elif kind == "flip":
            if arg not in (FlipType.Vertical, FlipType.Horizontal):
                raise ValueError("flip must be FlipType.Vertical or FlipType.Horizontal")
            ops.append({"op": "flip", "axis": arg})
        elif kind == "crop":
            x, y, cw, ch = (int(round(arg[k])) for k in ("originX", "originY", "width", "height"))
            if x < 0 or y < 0 or cw < 1 or ch < 1 or x + cw > w or y + ch > h:
                raise ValueError("crop %dx%d at (%d,%d) is outside the %dx%d image" % (cw, ch, x, y, w, h))
            ops.append({"op": "crop", "x": x, "y": y, "width": cw, "height": ch})
            w, h = cw, ch
        else:
            raise ValueError("unknown action %r (use resize, rotate, flip or crop)" % kind)
    return ops, w, h


_TEXT = ("make", "model", "lens", "software", "artist", "copyright", "description")
_NUMS = ("exposureTime", "fNumber", "focalLength", "latitude", "longitude", "altitude")


def tidy_metadata(raw: Dict[str, Any]) -> Info:
    """Cleans the native `metadata` reply: trims text, drops blanks, `dateTaken` as `YYYY-MM-DD HH:MM:SS`, `iso` as int,
    and adds `camera` (make + model, without repeating the make)."""
    out: Info = {}
    for k in _TEXT:
        v = raw.get(k)
        if isinstance(v, str) and v.strip("\x00 \t\r\n"):
            out[k] = v.strip("\x00 \t\r\n")
    for k in _NUMS:
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            out[k] = float(v)
    if not (-90 <= out.get("latitude", 0) <= 90 and -180 <= out.get("longitude", 0) <= 180) or ("latitude" in out) != ("longitude" in out):
        for k in ("latitude", "longitude", "altitude"):
            out.pop(k, None)
    iso = raw.get("iso")
    if isinstance(iso, (int, float)) and not isinstance(iso, bool) and math.isfinite(iso) and iso > 0:
        out["iso"] = int(round(iso))
    taken = raw.get("dateTaken")
    if isinstance(taken, str) and len(taken.strip()) >= 10 and taken[:4].isdigit():
        t = taken.strip()
        out["dateTaken"] = t[:10].replace(":", "-") + t[10:]
    make, model = out.get("make", ""), out.get("model", "")
    if make or model:
        out["camera"] = model if make and model.lower().startswith(make.lower()) else " ".join(x for x in (make, model) if x)
    return out


@snake_aliases
class ImageManipulator:
    @staticmethod
    async def getInfoAsync(uri: str) -> Info:
        """`{uri, width, height, fileSize, format, mimeType, hasExif, hasGps}` (width/height after EXIF orientation)."""
        return await call("info", str(uri))

    @staticmethod
    async def getMetadataAsync(uri: str) -> Info:
        """EXIF/GPS fields the file carries, for showing what a clean copy drops. Keys present only when found:
        `camera, make, model, lens, software, dateTaken, exposureTime, fNumber, iso, focalLength, artist, copyright,
        description, latitude, longitude, altitude`. `{}` for a file with none."""
        return tidy_metadata(await call("metadata", str(uri)) or {})

    @staticmethod
    async def manipulateAsync(uri: str, actions: Optional[List[Dict[str, Any]]] = None,
                              saveOptions: Optional[Dict[str, Any]] = None) -> Info:
        """Applies `actions` in order, then encodes. Returns `{uri, width, height, fileSize, format, mimeType}`
        (plus `base64` when `saveOptions["base64"]`).
        Actions: `{"resize": {width, height}}`, `{"rotate": degrees}` (multiples of 90), `{"flip": FlipType.*}`,
        `{"crop": {originX, originY, width, height}}`. saveOptions: `compress` 0-1 (default 1), `format`
        (default JPEG; WEBP is Android only, HEIC is iOS only), `base64`."""
        opts = saveOptions or {}
        fmt = opts.get("format", SaveFormat.JPEG)
        if fmt not in (SaveFormat.JPEG, SaveFormat.PNG, SaveFormat.WEBP, SaveFormat.HEIC):
            raise ValueError("bad format %r" % (fmt,))
        if fmt == SaveFormat.WEBP:
            require_supported("WEBP output", ios=False)
        if fmt == SaveFormat.HEIC:
            require_supported("HEIC output", android=False)
        compress = float(opts.get("compress", 1))
        if not 0 <= compress <= 1:
            raise ValueError("compress must be between 0 and 1")
        h = await call("open", str(uri), 0)
        try:
            ops, _w, _h = _plan(h["width"], h["height"], list(actions or []))
            out = await call("render", h["h"], {"ops": ops, "format": fmt, "quality": compress})
        finally:
            await call("close", h["h"])
        if opts.get("base64"):
            with open(out["uri"], "rb") as f:
                out["base64"] = base64.b64encode(f.read()).decode("ascii")
        return out

    @staticmethod
    async def compressToSizeAsync(uri: str, maxBytes: int, format: str = SaveFormat.JPEG, minEdge: int = 320) -> Info:
        """Re-encodes `uri` to at most `maxBytes`. Result has `quality` and `scale` when the search ran.
        Raises ImageTooLargeError when the target needs fewer than `minEdge` pixels on the long side."""
        if maxBytes <= 0:
            raise ValueError("maxBytes must be positive")
        if format == SaveFormat.WEBP:
            require_supported("WEBP output", ios=False)
        h = await call("open", str(uri), _MAX_EDGE)
        try:
            w, ht = h["width"], h["height"]

            async def render(q: float, scale: float) -> Info:
                spec: Dict[str, Any] = {"format": format, "quality": q}
                if scale < 1.0:
                    spec["width"], spec["height"] = max(1, int(w * scale)), max(1, int(ht * scale))
                return await call("render", h["h"], spec)

            return await search_under(render, int(maxBytes), max(w, ht), int(minEdge), lossy=format != SaveFormat.PNG)
        finally:
            await call("close", h["h"])
