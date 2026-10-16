"""Web backend for `pyxpo build web`: the desktop one, on Pillow running in the browser (Pyodide). The web build
installs Pillow there because the manifest lists it under `web.packages`."""
from .desktop import call  # noqa: F401
