from app_icon import APP_ICON_RELATIVE_PATH, get_app_icon_path, load_app_icon


def test_app_icon_asset_is_available_and_transparent():
    path = get_app_icon_path()

    assert path.is_file()
    assert path.parts[-2:] == APP_ICON_RELATIVE_PATH.parts
    image = load_app_icon()
    assert image.mode == "RGBA"
    assert image.size == (1254, 1254)
    assert image.getpixel((0, 0))[3] == 0


def test_app_icon_can_be_resized_for_native_icon_surfaces():
    image = load_app_icon(size=32)

    assert image.size == (32, 32)
    assert image.getbbox() is not None
