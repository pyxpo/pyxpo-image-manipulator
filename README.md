# pyxpo-image-manipulator

Resize, crop, rotate, flip, convert and compress images on the device, in [pyxpo](https://github.com/pyxpo/pyxpo) apps
written in Python. The API follows [expo-image-manipulator](https://docs.expo.dev/versions/latest/sdk/imagemanipulator/),
plus `compress_to_size_async` to get an image under a byte limit. The work runs natively on a background queue (Core
Graphics and ImageIO on iOS, Bitmap APIs on Android). Output files never carry EXIF or GPS data.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo/main/packages/image-manipulator/docs/images/hero.png" width="820" alt="An image resized, rotated and compressed on Android and iOS, with sizes before and after">
</p>

```python
from pyxpo_image_manipulator import ImageManipulator, SaveFormat

out = await ImageManipulator.manipulate_async(
    path, [{"resize": {"width": 800}}, {"rotate": 90}], {"compress": 0.8, "format": SaveFormat.JPEG})
```

## Features

- **Actions** applied in order: `resize`, `rotate` (multiples of 90), `flip`, `crop`.
- **Formats:** JPEG, PNG, WebP (Android) and HEIC (iOS), with a `compress` quality from 0 to 1.
- **Compress to a size:** `compress_to_size_async(uri, max_bytes)` finds the best quality, then shrinks the image if needed.
- **Info:** width and height (after EXIF rotation), size, format, and whether EXIF or GPS data exists.
- **Privacy:** results never contain EXIF or GPS.
- **Base64** output on request.
- **Everywhere:** Pillow does the work on the web page and the desktop window.
- **Typed:** ships `py.typed`. camelCase (`manipulateAsync`) and snake_case names both work.

## Contents

| If you are... | Start here |
| --- | --- |
| Shrinking a photo before upload | [Quick start](#quick-start) · [Compress to a size](#compress-to-a-size) |
| Cropping, rotating or flipping | [Actions](#actions) |
| Changing format | [Save options](#save-options) |
| Reading the size of a file | [Info](#info) |
| Exact signatures | [API reference](#api-reference) · [Errors](#errors) |
| Seeing it in a real app | [Recipe: upload a photo](#recipe-upload-a-photo) |
| Stuck | [Testing](#testing) · [Troubleshooting](#troubleshooting) · [FAQ](#faq) |

## Platform support

| Target | What works |
| --- | --- |
| Android, API 24+ | Everything. JPEG, PNG, WebP. No HEIC output. |
| iOS 15+ | Everything. JPEG, PNG, HEIC. No WebP output. |
| Web page and desktop window | Everything through Pillow. HEIC input on the Mac uses the system `sips` tool. |

Requires Python 3.9+ and `pyxpo>=0.1.0a1`. It needs no permissions and no Info.plist keys.

## Install

```sh
pip install pyxpo-image-manipulator
```

```toml
[tool.pyxpo]
modules = ["image-manipulator"]
```

Rebuild the app. The web build installs Pillow for you.

## Quick start

```python
from pyxpo import Button, Image, Text, View, component, use_async, use_state
from pyxpo_image_manipulator import ImageManipulator, SaveFormat


@component
def Shrink(path: str = ""):
    taps, set_taps = use_state(0)
    out, set_out = use_state(None)

    async def run():
        if taps == 0 or not path:
            return
        info = await ImageManipulator.get_info_async(path)
        res = await ImageManipulator.manipulate_async(
            path,
            [{"resize": {"width": min(800, info["width"])}}],
            {"compress": 0.7, "format": SaveFormat.JPEG},
        )
        set_out(res)

    use_async(run, [taps])

    return View(
        Image(source=out["uri"], style={"height": 200}) if out else Text("No result yet"),
        Text("%s x %s, %s bytes" % (out["width"], out["height"], out["fileSize"]) if out else ""),
        Button("Shrink", lambda: set_taps(taps + 1)),
    )
```

> **Tip:** Start async work from a button with a counter in state and `use_async(fn, [counter])`. A
> `lambda: coroutine()` is never awaited.

<p align="center">
  <img src="https://raw.githubusercontent.com/pyxpo/pyxpo/main/packages/image-manipulator/docs/images/result.png" width="720" alt="The original photo, then the resized and rotated copy with its new size, on Android and iOS">
</p>

## Info

```python
info = await ImageManipulator.get_info_async(path)
# {"uri", "width", "height", "fileSize", "format", "mimeType", "hasExif", "hasGps"}
```

`width` and `height` are after EXIF orientation. Use `hasGps` to warn before sharing an original photo.

## Actions

`manipulate_async(uri, actions, save_options)` runs a list of one-key dicts in order. The size is tracked, so a crop
after a rotate uses the rotated size.

| Action | Value | Notes |
| --- | --- | --- |
| `resize` | `{"width": 800}` or `{"height": 600}` or both | One side keeps the aspect ratio. Both sides force that exact size. |
| `rotate` | degrees | Multiples of 90 only. Anything else raises `NotSupportedError`. |
| `flip` | `FlipType.Horizontal` or `FlipType.Vertical` | |
| `crop` | `{"originX", "originY", "width", "height"}` | In pixels, inside the current image, or `ValueError`. |

```python
out = await ImageManipulator.manipulate_async(path, [
    {"rotate": 90},
    {"crop": {"originX": 0, "originY": 0, "width": 500, "height": 500}},
    {"resize": {"width": 256}},
    {"flip": FlipType.Horizontal},
])
```

To crop to a ratio such as 4:5, use the helpers `centered_crop(width, height, parse_ratio("4:5"))`, which return the
`x`, `y`, `width` and `height` of the largest centered rectangle. Map them to `originX` and `originY`.

## Save options

| Option | Notes |
| --- | --- |
| `compress` | 0 to 1, default 1. Used by JPEG, WebP and HEIC (PNG is lossless). |
| `format` | `SaveFormat.JPEG` (default), `PNG`, `WEBP` (Android only), `HEIC` (iOS only). |
| `base64` | `True` adds a `base64` string to the result. |

The result is `{"uri", "width", "height", "fileSize", "format", "mimeType"}` (plus `base64`). The `uri` is a new file;
the original is not changed.

## Compress to a size

```python
small = await ImageManipulator.compress_to_size_async(path, 200_000)               # JPEG, bytes
png = await ImageManipulator.compress_to_size_async(path, 500_000, format=SaveFormat.PNG)
```

It tries the best quality first (0.92). If the file is too big it searches quality down to 0.30. If that is still too big
it shrinks the image and searches again, up to five times. The result carries `quality` and `scale` when a search ran.
If the long side would go below `min_edge` (default 320 px), it raises `ImageTooLargeError`. The search decodes at most 4096 px
on the long side to keep memory in check. For PNG there is no quality, so only the size shrinks.

## Errors

| Error | When | What to do |
| --- | --- | --- |
| `ValueError` | Bad format, `compress` outside 0 to 1, an action that is not a one-key dict, an unknown action, a crop outside the image, `max_bytes <= 0`. | Fix the call. |
| `NotSupportedError` | A rotation that is not a multiple of 90, WebP on iOS, HEIC on Android. | Use JPEG or PNG. |
| `ImageTooLargeError` | `compress_to_size_async` cannot reach the size above `min_edge`. | Raise `max_bytes` or lower `min_edge`. |
| `ModuleNotLinkedError` (from `pyxpo.module_api`) | Built without `"image-manipulator"` in `modules`. | Add it and rebuild. |
| A native error (file missing, not an image) | Bad `uri`. | Check the path. |

## Config

None. No permissions, no Info.plist keys.

## Web and desktop

Pillow does the work, with the same API. On the web page the module asks the build to install Pillow. On the Mac, HEIC
files are read through the system `sips` tool. Output goes to `~/.cache/pyxpo/images` (or `$XDG_CACHE_HOME`) on the
desktop.

## Recipe: upload a photo

Get any picked photo under 300 KB without GPS data, then send it.

```python
from pyxpo import Button, Text, View, component, use_async, use_state
from pyxpo_fetch import fetch
from pyxpo_image_manipulator import ImageManipulator, ImageTooLargeError
from pyxpo_image_picker import ImagePicker


@component
def UploadPhoto():
    taps, set_taps = use_state(0)
    status, set_status = use_state("")

    async def run():
        if taps == 0:
            return
        res = await ImagePicker.launch_image_library_async()
        if res["canceled"]:
            return
        src = res["assets"][0]["uri"]
        try:
            small = await ImageManipulator.compress_to_size_async(src, 300_000)
        except ImageTooLargeError:
            set_status("That photo is too detailed to shrink enough")
            return
        set_status("Ready: %d bytes (quality %s)" % (small["fileSize"], small.get("quality", 1)))
        # send small["uri"] with your upload code, for example through pyxpo_fetch.fetch(...)

    use_async(run, [taps])
    return View(Button("Choose and shrink", lambda: set_taps(taps + 1)), Text(status))
```

## Production checklist

- [ ] You handle `ImageTooLargeError` and offer a clear message.
- [ ] You check `hasGps` or rely on the result having none (results never keep EXIF or GPS).
- [ ] You delete temporary results when finished (they are in the app cache).
- [ ] You do not ask for WebP on iOS or HEIC on Android.
- [ ] You tested a very large photo (12 MP and up) on a real phone.
- [ ] You gave a loading state: big images take a moment.

## Testing

The native side has four primitives (`info`, `open`, `render`, `close`), so you can fake them. The plain Python parts
need no fake at all:

```python
import asyncio

import pyxpo_image_manipulator as mod


def test_ratio_crop():
    box = mod.centered_crop(1000, 500, mod.parse_ratio("1:1"))
    assert box == {"x": 250, "y": 0, "width": 500, "height": 500}


def test_size_search_shrinks():
    async def render(q, scale):
        return {"uri": "/nonexistent", "fileSize": int(1_000_000 * q * scale)}

    out = asyncio.run(mod.search_under(render, 300_000, 4000, 320))
    assert out["fileSize"] <= 300_000


def test_manipulate_with_fake(monkeypatch):
    seen = []

    async def fake_call(method, *args):
        seen.append(method)
        if method == "open":
            return {"h": 1, "width": 400, "height": 200}
        if method == "render":
            return {"uri": "/x.jpg", "width": 200, "height": 400, "fileSize": 5, "format": "jpeg"}

    monkeypatch.setattr(mod, "call", fake_call)
    out = asyncio.run(mod.ImageManipulator.manipulate_async("/a.jpg", [{"rotate": 90}]))
    assert out["width"] == 200 and seen == ["open", "render", "close"]
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `NotSupportedError: rotate supports multiples of 90` | Rotate in steps of 90. |
| `NotSupportedError` for WEBP or HEIC | WebP is Android only, HEIC is iOS only. |
| `ValueError: crop ... is outside` | The crop is bigger than the image after earlier actions. Check `get_info_async`. |
| `ImageTooLargeError` | Raise the byte limit, or lower `min_edge`. |
| The picture is rotated wrong | `width` and `height` are after EXIF orientation, and results are written upright. |
| `ModuleNotLinkedError` | Add `"image-manipulator"` to `modules` and rebuild. |

## FAQ

**Is the original changed?** No. A new file is written.

**Does the output keep GPS?** No. EXIF and GPS are stripped.

**Why do `compress_to_size_async` results have `scale`?** A scale below 1 means quality alone could not reach the limit
and the image was made smaller.

**Can it rotate by 45 degrees?** No, multiples of 90 only.

## API reference

### `ImageManipulator`

| Method (camelCase name) | Returns |
| --- | --- |
| `get_info_async(uri)` | `{uri, width, height, fileSize, format, mimeType, hasExif, hasGps}` |
| `manipulate_async(uri, actions=None, save_options=None)` | `{uri, width, height, fileSize, format, mimeType}` (+ `base64`) |
| `compress_to_size_async(uri, max_bytes, format="jpeg", min_edge=320)` | The same, plus `quality` and `scale` when a search ran. |

### Other names

| Name | Description |
| --- | --- |
| `SaveFormat` | `JPEG`, `PNG`, `WEBP` (Android), `HEIC` (iOS). |
| `FlipType` | `Horizontal`, `Vertical`. |
| `ImageTooLargeError` | Raised when the size target is out of reach. |
| `parse_ratio("4:5")`, `centered_crop(w, h, ratio)` | Helpers for ratio crops. |
| `search_under(render, max_bytes, longest, min_edge)` | The size search, usable with your own `render`. |

## License

MIT
