from pathlib import Path

from PIL import Image

from app_icon import APP_ICON_RELATIVE_PATH, get_app_icon_path, load_app_icon


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_app_icon_asset_is_available_and_transparent():
    path = get_app_icon_path()

    assert path.is_file()
    assert path.parts[-2:] == APP_ICON_RELATIVE_PATH.parts
    image = load_app_icon()
    assert image.mode == "RGBA"
    assert image.size == (1310, 1310)
    assert image.getpixel((0, 0))[3] == 0


def test_app_icon_can_be_resized_for_native_icon_surfaces():
    image = load_app_icon(size=32)

    assert image.size == (32, 32)
    assert image.getbbox() is not None


def test_windows_icon_contains_all_expected_native_sizes():
    with Image.open(PROJECT_ROOT / "assets" / "app_icon.ico") as image:
        assert image.format == "ICO"
        assert image.ico.sizes() == {
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        }


def test_social_preview_matches_github_recommended_canvas_and_size_limit():
    path = PROJECT_ROOT / "assets" / "social_preview.jpg"

    assert path.stat().st_size < 1_000_000
    with Image.open(path) as image:
        assert image.format == "JPEG"
        assert image.size == (1280, 640)
