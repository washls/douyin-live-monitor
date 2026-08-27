import threading

from tray_controller import TrayController, create_tray_image


class FakeMenuItem:
    def __init__(self, text, action, **kwargs):
        self.text = text
        self.action = action
        self.enabled = kwargs.get("enabled", True)
        self.default = kwargs.get("default", False)


class FakeMenu:
    def __init__(self, *items):
        self.items = items


class FakeIcon:
    def __init__(self, _name, icon, title, menu):
        self.image = icon
        self.title = title
        self.menu = menu
        self.visible = False
        self.updated = 0
        self.stopped = threading.Event()

    def run(self, setup):
        setup(self)
        self.stopped.wait()

    def update_menu(self):
        self.updated += 1

    def stop(self):
        self.stopped.set()


class FakePystray:
    Menu = FakeMenu
    MenuItem = FakeMenuItem
    Icon = FakeIcon


def test_tray_image_uses_the_existing_blue_and_red_identity():
    image = create_tray_image()

    assert image.size == (64, 64)
    assert image.getpixel((8, 8))[:3] == (0, 103, 192)
    assert image.getpixel((32, 32))[:3] == (196, 43, 28)


def test_tray_menu_order_dynamic_state_and_callbacks():
    events = []
    ready = threading.Event()

    def sink(event):
        events.append(event)
        if event["type"] == "tray_ready":
            ready.set()

    controller = TrayController(sink, pystray_module=FakePystray)
    assert controller.start() is True
    assert ready.wait(timeout=1)

    items = controller.icon.menu.items
    labels = [item.text(item) if callable(item.text) else item.text for item in items]
    assert labels == [
        "打开主窗口",
        "开始监控",
        "停止监控",
        "当前在线主播数量：0",
        "退出程序",
    ]
    assert items[0].default is True
    assert items[1].enabled(items[1]) is True
    assert items[2].enabled(items[2]) is False

    controller.update_state(True, 3)
    assert items[1].enabled(items[1]) is False
    assert items[2].enabled(items[2]) is True
    assert items[3].text(items[3]) == "当前在线主播数量：3"
    assert controller.icon.updated == 1

    for item in (items[0], items[1], items[2], items[4]):
        item.action(controller.icon, item)
    assert [event["type"] for event in events[-4:]] == [
        "tray_open",
        "tray_start",
        "tray_stop",
        "tray_exit",
    ]
    controller.stop()
