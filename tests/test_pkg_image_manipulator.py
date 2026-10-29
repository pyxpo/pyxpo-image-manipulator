from __future__ import annotations

import asyncio

import pytest

from pyxpo.apis import _bridge
from pyxpo.module_api import NotSupportedError
from pyxpo_image_manipulator import FlipType, ImageManipulator, ImageTooLargeError, SaveFormat
from pyxpo_image_manipulator import centered_crop, fit_size, parse_ratio, search_under, tidy_metadata


class FakeApp:
    """Records native calls and answers from `handlers[(module, method)]`."""

    loop = None

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def call(self, module, method, *args, reply=False):
        self.calls.append((module, method, args))
        fut = asyncio.get_event_loop().create_future()
        out = self.handlers[(module, method)](*args)
        if isinstance(out, Exception):
            fut.set_exception(out)
        else:
            fut.set_result(out)
        return fut


def run(handlers, coro_fn):
    app = FakeApp(handlers)
    old = _bridge.get_app()
    _bridge.set_app(app)
    try:
        return asyncio.run(coro_fn()), app
    finally:
        _bridge.set_app(old)


def test_ratio_and_crop_math():
    assert parse_ratio("4:5") == 0.8
    with pytest.raises(ValueError):
        parse_ratio("square")
    assert centered_crop(4000, 3000, 1.0) == {"x": 500, "y": 0, "width": 3000, "height": 3000}
    assert centered_crop(3000, 4000, 16 / 9) == {"x": 0, "y": 1156, "width": 3000, "height": 1688}


def test_fit_size():
    assert fit_size(4000, 3000, {"width": 1080, "height": 1080}) == (1080, 1080)
    assert fit_size(4000, 3000, {"width": 1080, "height": 1080, "mode": "fit"}) == (1080, 810)
    assert fit_size(4000, 3000, {"width": 2000}) == (2000, 1500)
    with pytest.raises(ValueError):
        fit_size(10, 10, {})


def _model(tmp_path, base=4_000_000, floor=0.0):
    """Fake renderer: size ~ base * quality^2 * scale^2 (+floor); writes a real file so discards are observable."""
    files = []

    async def render(q, scale):
        p = tmp_path / ("r%d.jpg" % len(files))
        p.write_bytes(b"x")
        files.append(p)
        return {"uri": str(p), "fileSize": int(base * q * q * scale * scale + floor)}

    return render, files


def test_search_returns_first_try_when_it_fits(tmp_path):
    render, files = _model(tmp_path, base=1_000_000)
    out = asyncio.run(search_under(render, 2_000_000, 4000, 320))
    assert out["fileSize"] <= 2_000_000 and len(files) == 1


def test_search_picks_highest_quality_that_fits_and_cleans_up(tmp_path):
    render, files = _model(tmp_path)
    target = 1_000_000
    out = asyncio.run(search_under(render, target, 4000, 320))
    assert out["fileSize"] <= target and out["scale"] == 1.0
    assert 0.45 < out["quality"] < 0.52  # sqrt(1/4) = 0.5, found by bisection
    assert [p for p in files if p.exists()] == [__import__("pathlib").Path(out["uri"])]


def test_search_scales_down_when_quality_floor_is_not_enough(tmp_path):
    render, files = _model(tmp_path, base=40_000_000)
    out = asyncio.run(search_under(render, 500_000, 4000, 320))
    assert out["fileSize"] <= 500_000 and out["scale"] < 1.0
    assert sum(p.exists() for p in files) == 1


def test_search_gives_up_below_min_edge(tmp_path):
    render, files = _model(tmp_path, base=1_000, floor=10_000_000)
    with pytest.raises(ImageTooLargeError):
        asyncio.run(search_under(render, 1000, 4000, 320))
    assert not any(p.exists() for p in files)


def test_search_png_only_scales(tmp_path):
    files = []

    async def render(q, scale):
        assert q == 1.0
        p = tmp_path / ("p%d.png" % len(files))
        p.write_bytes(b"x")
        files.append(p)
        return {"uri": str(p), "fileSize": int(9_000_000 * scale * scale)}

    out = asyncio.run(search_under(render, 2_000_000, 4000, 320, lossy=False))
    assert out["fileSize"] <= 2_000_000 and sum(p.exists() for p in files) == 1


def _tools(log):
    def open_(path, max_edge):
        log.append(("open", path, max_edge))
        return {"h": 7, "width": 4000, "height": 3000, "sourceWidth": 8000, "sourceHeight": 6000}

    def render(h, spec):
        log.append(("render", h, spec))
        return {"uri": "/o.jpg", "fileSize": 10, "width": 1, "height": 1}

    def close(h):
        log.append(("close", h))

    return {("ImageManipulator", "open"): open_, ("ImageManipulator", "render"): render, ("ImageManipulator", "close"): close}


def test_manipulate_plans_ops_in_order_and_always_closes():
    log = []
    out, _ = run(_tools(log), lambda: ImageManipulator.manipulateAsync(
        "/a.heic", [{"rotate": 90}, {"resize": {"width": 1500}}, {"crop": {"originX": 0, "originY": 0, "width": 100, "height": 100}},
                    {"flip": FlipType.Horizontal}], {"compress": 0.8, "format": SaveFormat.PNG}))
    assert out["uri"] == "/o.jpg"
    assert log[0] == ("open", "/a.heic", 0)
    spec = log[1][2]
    # 4000x3000 rotated 90 -> 3000x4000; width 1500 keeps aspect -> 1500x2000
    assert spec["ops"] == [{"op": "rotate", "degrees": 90}, {"op": "resize", "width": 1500, "height": 2000},
                           {"op": "crop", "x": 0, "y": 0, "width": 100, "height": 100}, {"op": "flip", "axis": "horizontal"}]
    assert (spec["format"], spec["quality"]) == ("png", 0.8)
    assert log[-1] == ("close", 7)


def test_manipulate_validates_before_render():
    log = []
    for actions, exc in (([{"crop": {"originX": 3900, "originY": 0, "width": 200, "height": 10}}], ValueError),
                         ([{"rotate": 45}], NotSupportedError), ([{"spin": 1}], ValueError), ([{"resize": {}}], ValueError)):
        with pytest.raises(exc):
            run(_tools(log), lambda: ImageManipulator.manipulateAsync("/a.jpg", actions))
    assert all(c[0] != "render" for c in log) and log.count(("close", 7)) == 4
    with pytest.raises(ValueError):
        run(_tools(log), lambda: ImageManipulator.manipulateAsync("/a.jpg", [], {"compress": 2}))


def test_manipulate_not_supported_formats_raise_before_native_call(monkeypatch):
    from pyxpo.apis import _env
    log = []
    monkeypatch.setattr(_env, "current", _env.Env({"os": "ios", "modules": ["ImageManipulator"]}))
    with pytest.raises(NotSupportedError):
        run(_tools(log), lambda: ImageManipulator.manipulateAsync("/a.jpg", [], {"format": SaveFormat.WEBP}))
    monkeypatch.setattr(_env, "current", _env.Env({"os": "android", "modules": ["ImageManipulator"]}))
    with pytest.raises(NotSupportedError):
        run(_tools(log), lambda: ImageManipulator.manipulateAsync("/a.jpg", [], {"format": SaveFormat.HEIC}))
    assert log == []


def test_manipulate_base64(tmp_path):
    f = tmp_path / "o.jpg"
    f.write_bytes(b"abc")
    h = _tools([])
    h[("ImageManipulator", "render")] = lambda hh, spec: {"uri": str(f), "fileSize": 3}
    out, _ = run(h, lambda: ImageManipulator.manipulateAsync("/a.jpg", [], {"base64": True}))
    assert out["base64"] == "YWJj"


def test_manipulate_closes_handle_on_native_error():
    log = []
    h = _tools(log)
    h[("ImageManipulator", "render")] = lambda hh, spec: RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        run(h, lambda: ImageManipulator.manipulateAsync("/a.jpg"))
    assert ("close", 7) in log


def test_compress_to_size_uses_search_and_closes(tmp_path):
    log = []
    h = _tools(log)
    n = []

    def render(hh, spec):
        p = tmp_path / ("c%d.jpg" % len(n))
        p.write_bytes(b"x")
        n.append(p)
        return {"uri": str(p), "fileSize": int(4_000_000 * spec["quality"] ** 2)}

    h[("ImageManipulator", "render")] = render
    out, _ = run(h, lambda: ImageManipulator.compressToSizeAsync("/a.jpg", 1_000_000))
    assert out["fileSize"] <= 1_000_000
    assert log[0] == ("open", "/a.jpg", 4096) and log[-1] == ("close", 7)
    with pytest.raises(ValueError):
        run(h, lambda: ImageManipulator.compressToSizeAsync("/a.jpg", 0))


def test_metadata_is_tidied():
    raw = {"make": "Apple", "model": "iPhone 15 Pro", "software": "  ", "lens": "back camera\x00", "dateTaken": "2024:05:01 10:22:33",
           "exposureTime": 1 / 120, "iso": 49.6, "latitude": 28.6, "longitude": 77.2, "altitude": float("nan")}
    out, app = run({("ImageManipulator", "metadata"): lambda uri: raw}, lambda: ImageManipulator.getMetadataAsync("/a.jpg"))
    assert app.calls == [("ImageManipulator", "metadata", ("/a.jpg",))]
    assert out == {"make": "Apple", "model": "iPhone 15 Pro", "camera": "Apple iPhone 15 Pro",
                   "lens": "back camera", "dateTaken": "2024-05-01 10:22:33", "exposureTime": 1 / 120, "iso": 50,
                   "latitude": 28.6, "longitude": 77.2}


def test_metadata_camera_skips_repeated_make_and_bad_location():
    out = tidy_metadata({"make": "Google", "model": "Google Pixel 8", "latitude": 120.0, "longitude": 5.0, "altitude": 3.0})
    assert out == {"make": "Google", "model": "Google Pixel 8", "camera": "Google Pixel 8"}
    assert tidy_metadata({}) == {}
    assert tidy_metadata({"latitude": 1.0}) == {}  # half a location is no location


def test_desktop_metadata_reads_exif_and_gps(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from pyxpo_image_manipulator import desktop
    ex = Image.Exif()
    ex[0x010F], ex[0x0110] = "Apple", "iPhone 15 Pro"
    ex.get_ifd(0x8769).update({0x9003: "2024:05:01 10:22:33", 0x829D: 1.8, 0x8827: 50})
    ex.get_ifd(0x8825).update({1: "S", 2: (28.0, 30.0, 0.0), 3: "W", 4: (77.0, 0.0, 0.0), 5: b"\x01", 6: 10.0})
    p = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(p, exif=ex)
    out = tidy_metadata(desktop.metadata(str(p)))
    assert out["camera"] == "Apple iPhone 15 Pro" and out["dateTaken"] == "2024-05-01 10:22:33"
    assert (out["fNumber"], out["iso"]) == (1.8, 50)
    assert (out["latitude"], out["longitude"], out["altitude"]) == (-28.5, -77.0, -10.0)
    q = tmp_path / "b.png"
    Image.new("RGB", (8, 8)).save(q)
    assert desktop.metadata(str(q)) == {}
