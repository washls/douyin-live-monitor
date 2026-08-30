"""Shared application-icon loading for source and frozen builds."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional


APP_ICON_RELATIVE_PATH = Path("assets") / "app_icon.png"


def get_app_icon_path() -> Path:
    """Return the bundled or source-tree application icon path."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / APP_ICON_RELATIVE_PATH
    return Path(__file__).resolve().parent.parent / APP_ICON_RELATIVE_PATH


def load_app_icon(
    image_module: Any = None,
    size: Optional[int] = None,
) -> Any:
    """Load the shared RGBA icon, optionally resized to a square pixel size."""
    if image_module is None:
        from PIL import Image

        image_module = Image

    with image_module.open(get_app_icon_path()) as source:
        image = source.convert("RGBA")

    if size is not None and image.size != (size, size):
        resampling = getattr(image_module, "Resampling", image_module)
        image = image.resize((size, size), resampling.LANCZOS)
    return image
