#!/usr/bin/env python3
"""
Douyin Live Monitor with Server酱³ Push Notifications

Monitors multiple Douyin streamers and sends push notifications via
Server酱³ when their live status changes.

Usage:
    python monitor.py                  # Run with config.json
    python monitor.py --once           # Single check
    python monitor.py --config my.json # Custom config
"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from douyin_client import DouyinClient
from monitor_service import MonitorService
from notifier import ServerChanNotifier
from streamer_config import (
    add_streamer,
    enabled_streamers,
    migrate_legacy_streamer,
    normalize_streamers,
    remove_streamer,
    save_config_atomic,
    set_streamer_enabled,
    validate_streamer_url,
)

# ===== Path Helpers (supports PyInstaller frozen exe) =====

def _get_runtime_dir() -> Path:
    """Get the directory where the executable/config lives.

    When frozen by PyInstaller, sys.executable is the .exe path.
    When running as script, __file__ is the .py path.
    """
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# ===== Constants =====
APP_VERSION = "1.3.1"
BASE_DIR = _get_runtime_dir()
DEFAULT_CONFIG = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "monitor.log"
STATE_FILE = BASE_DIR / ".monitor_state.json"

# ===== Logging Setup =====


def _configure_console_output(stream) -> None:
    """Use UTF-8 on Windows without replacing or closing the stdio stream."""
    if sys.platform != "win32" or not hasattr(stream, "reconfigure"):
        return
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure dual-output logging.

    - Console: WARNING level by default (only errors / warnings are shown).
      Use --verbose to raise the console level to DEBUG.
    - File: always DEBUG — every check, every method detail is saved.

    Idempotent: if handlers are already configured, returns immediately.
    """
    root = logging.getLogger()
    if root.handlers:
        return logging.getLogger("douyin_monitor")

    root.setLevel(logging.DEBUG)

    # --- console handler ------------------------------------------------
    _configure_console_output(sys.stdout)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    # --- file handler --------------------------------------------------
    # Keep verbose diagnostics available without allowing monitor.log to grow
    # indefinitely during long-running monitoring.
    file_handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root.addHandler(console)
    root.addHandler(file_handler)

    # urllib3 DEBUG output includes full request URLs. Push URLs contain the
    # SendKey, so third-party connection logs must never be written verbatim.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return logging.getLogger("douyin_monitor")


logger = setup_logging()


# ── Tiny helper: print to terminal AND write to log in one call ──
def _echo(level: int, msg: str) -> None:
    """Log at *level* (→ file always, → console only if WARNING+)."""
    logger.log(level, msg)

# ===== State Management =====


class MonitorState:
    """Tracks the monitoring state to avoid duplicate notifications.

    Notification behavior:
      - First detection of going live → push immediately.
      - While still live → push every N seconds (repeat_notify_interval),
        up to max_repeat_notifications times.
      - New stream (different room_id) → reset and treat as new "went_live".
    """

    UNKNOWN = "unknown"
    OFFLINE = "offline"
    LIVE = "live"

    def __init__(self):
        self.status = self.UNKNOWN
        self.last_live_start: Optional[float] = None
        self.last_status_change: float = time.time()
        self.streamer_nickname: str = ""
        self.stream_room_id: str = ""
        self.stream_title: str = ""
        self.notification_sent: bool = False
        # Repeat notification tracking (while still live)
        self.repeat_notify_count: int = 0
        self.last_repeat_notify_time: float = 0.0
        self.max_repeat_notifications: int = 3
        self.repeat_notify_interval: int = 600  # 10 minutes in seconds
        # Daily intimacy reminder tracking
        self._daily_reminder_minutes_sent: set = set()  # e.g. {57, 58, 59}
        self._daily_reminder_date: str = ""  # track date to reset on new day

    def transition(self, is_live: bool, info: Dict[str, Any]) -> str:
        """
        Update state based on current live status.
        Returns 'went_live', 'went_offline', 'still_live', or 'no_change'.
        """
        nickname = info.get("nickname", self.streamer_nickname)
        room_id = info.get("room_id", "")
        title = info.get("title", "")

        if is_live:
            # Snapshot old room_id BEFORE updating, so we can detect new streams
            old_room_id = self.stream_room_id

            if nickname:
                self.streamer_nickname = nickname
            if room_id:
                self.stream_room_id = room_id
            if title:
                self.stream_title = title

            if self.status == self.LIVE:
                # Still live — but reset if stream changed (new room = new stream)
                if room_id and old_room_id and room_id != old_room_id:
                    logger.info("检测到新直播间 (可能是新一轮直播)")
                    self.notification_sent = False
                    self.repeat_notify_count = 0
                    self.last_repeat_notify_time = 0.0
                    return "went_live"

                # Still live, same room — check for repeat notification
                return "still_live"

            # Transition: OFFLINE/UNKNOWN -> LIVE
            logger.info(f"状态变更: {self.status} -> LIVE")
            self.status = self.LIVE
            self.last_live_start = time.time()
            self.last_status_change = time.time()
            self.notification_sent = False
            self.repeat_notify_count = 0
            self.last_repeat_notify_time = 0.0
            return "went_live"

        else:
            if self.status != self.LIVE:
                # Still offline
                if self.status == self.UNKNOWN:
                    self.status = self.OFFLINE
                # Update nickname cache
                if nickname:
                    self.streamer_nickname = nickname
                return "no_change"

            # Transition: LIVE -> OFFLINE
            duration = self._format_duration()
            logger.info(
                f"状态变更: LIVE -> OFFLINE (直播时长: {duration})"
            )
            self.status = self.OFFLINE
            self.last_status_change = time.time()
            return "went_offline"

    def _format_duration(self) -> str:
        """Format the current live duration as a human-readable string."""
        if not self.last_live_start:
            return ""
        secs = int(time.time() - self.last_live_start)
        hours, remainder = divmod(secs, 3600)
        mins, secs = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours}小时")
        if mins:
            parts.append(f"{mins}分钟")
        parts.append(f"{secs}秒")
        return "".join(parts)

    def should_notify_first_live(self) -> bool:
        """Check if we should send the FIRST live notification.

        Always sends on first detection; no cooldown.
        """
        return not self.notification_sent

    def mark_first_notification_sent(self) -> None:
        """Commit the first-live notification only after a successful send."""
        self.notification_sent = True
        self.last_repeat_notify_time = time.time()

    def should_notify_repeat(self) -> bool:
        """Check if we should send a REPEAT notification while still live.

        Returns True every `repeat_notify_interval` seconds,
        up to `max_repeat_notifications` times.
        """
        if self.status != self.LIVE or not self.notification_sent:
            return False
        if self.repeat_notify_count >= self.max_repeat_notifications:
            return False
        if time.time() - self.last_repeat_notify_time < self.repeat_notify_interval:
            return False
        self.repeat_notify_count += 1
        self.last_repeat_notify_time = time.time()
        logger.info(
            f"重复推送 ({self.repeat_notify_count}/"
            f"{self.max_repeat_notifications})"
        )
        return True

    def get_summary(self) -> str:
        """Get a human-readable status summary."""
        status_icons = {
            self.UNKNOWN: "[?]",
            self.OFFLINE: "[O]",
            self.LIVE: "[LIVE]",
        }
        icon = status_icons.get(self.status, "[?]")
        nickname = self.streamer_nickname or "未知博主"
        return f"{icon} [{self.status.upper()}] {nickname}"


# ===== Configuration =====


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load configuration from JSON file. Create default if not exists."""
    if not config_path.exists():
        logger.info(f"配置文件不存在，将创建默认配置: {config_path}")
        config = _default_config()
        try:
            save_config_atomic(config_path, config)
        except OSError as e:
            logger.warning(f"创建配置文件失败: {e}")
        return config

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    return config


def _default_config() -> Dict[str, Any]:
    """Return default configuration values."""
    return {
        "sendkey": "",
        "push_uid": "",
        "push_url": "",
        "check_interval": 30,
        "notify_on_stream_end": True,
        "repeat_notify_interval": 600,
        "max_repeat_notifications": 3,
        "startup_notify": False,
        "enable_daily_intimacy_reminder": True,
        "max_concurrent_checks": 2,
        "streamers": [],
    }


def _pause_if_frozen() -> None:
    """Pause before exit when running as a frozen PyInstaller exe.

    This keeps the console window open so the user can read any error
    messages before the window disappears.
    """
    if getattr(sys, 'frozen', False):
        try:
            input("按 Enter 键退出...")
        except (EOFError, KeyboardInterrupt):
            pass


def _create_notifier(config: Dict[str, Any]) -> ServerChanNotifier:
    """Create a ServerChanNotifier from a configuration dict."""
    return ServerChanNotifier(
        sendkey=config.get("sendkey", ""),
        uid=config.get("push_uid"),
        push_url=config.get("push_url"),
    )


def is_serverchan_configured(config: Dict[str, Any]) -> bool:
    """Check if Server酱 is properly configured."""
    push_url = config.get("push_url")
    if push_url and push_url != "YOUR_PUSH_URL_HERE":
        try:
            ServerChanNotifier._parse_push_url(str(push_url))
            return True
        except ValueError:
            return False
    # Check if sendkey is set
    if config.get("sendkey") and config["sendkey"] not in ("", "YOUR_SENDKEY_HERE"):
        return True
    return False


def prompt_serverchan_config(config_path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    First-launch prompt: ask the user for their Server酱³ push URL.

    Only called when no valid Server酱 configuration is found.
    Saves the config back to disk after entry.

    Returns:
        Updated config dict.
    """
    print()
    print("=" * 55)
    print("       Server酱³ 推送配置 (首次运行)")
    print("=" * 55)
    print()
    print("  需要配置 Server酱³ 才能接收开播通知到手机。")
    print()
    print("  获取方式:")
    print("    1. 访问 https://sc3.ft07.com 微信扫码登录")
    print("    2. 进入「发送消息」页面")
    print("    3. 复制你的完整推送 URL")
    print("       (格式: https://你的ID.push.ft07.com/send/你的SendKey.send)")
    print()
    print("  如果暂时不需要推送功能，可直接按 Enter 跳过。")
    print()

    while True:
        try:
            url = input("请粘贴 Server酱³ 推送 URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消，跳过推送配置")
            url = ""

        if not url:
            print()
            print("⚠️  未配置 Server酱³，将无法发送手机通知。")
            print("   以后可以在 config.json 中手动配置。")
            print()
            break

        try:
            ServerChanNotifier._parse_push_url(url)
        except ValueError:
            print("❌ URL 格式不正确，请粘贴完整的 HTTPS 推送 URL")
            print("   格式示例: https://12345.push.ft07.com/send/SCTxxxxxx.send")
            print()
        else:
            config["push_url"] = url
            config["sendkey"] = ""  # Will be parsed from URL
            config["push_uid"] = ""  # Will be parsed from URL

            # Save config
            try:
                save_config_atomic(config_path, config)
                print()
                print("✅ Server酱³ 配置已保存!")
                print()
            except OSError as e:
                logger.warning(f"保存配置失败: {e}")
            break

    return config


# ===== Monitor State Persistence =====


def load_monitor_state() -> Dict[str, Any]:
    """Load the previously monitored streamer info from state file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state
        except (json.JSONDecodeError, KeyError):
            logger.warning("状态文件损坏，将重新创建")
    return {}


def save_monitor_state(target_url: str, nickname: str = "") -> None:
    """Save the current monitored streamer info to state file."""
    state = {
        "target_url": target_url,
        "nickname": nickname,
        "last_monitored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.debug(f"监控状态已保存: {STATE_FILE}")
    except OSError as e:
        logger.warning(f"保存状态文件失败: {e}")


# ===== Interactive Target URL Prompt =====


def prompt_target_url() -> str:
    """
    Prompt the user to choose which streamer to monitor.

    On first run: asks for the streamer's profile link.
    On subsequent runs: shows the previous streamer and asks
    whether to continue or switch to a new one.

    Returns:
        The target URL to monitor.
    """
    prev_state = load_monitor_state()
    prev_url = prev_state.get("target_url", "")
    prev_nick = prev_state.get("nickname", "")
    prev_time = prev_state.get("last_monitored_at", "")

    print()
    print("=" * 50)
    print(f"         Douyin Live Monitor  v{APP_VERSION}")
    print("=" * 50)
    print()

    if prev_url:
        # Previous streamer exists — ask to continue or switch
        print("📌 上次监控的主播:")
        if prev_nick:
            print(f"   昵称: {prev_nick}")
        print(f"   链接: {prev_url}")
        if prev_time:
            print(f"   上次监控时间: {prev_time}")
        print()
        print("是否继续监控该主播？")
        print("  [Y] 是，继续监控")
        print("  [N] 否，切换新主播")
        print("  [Q] 退出")
        print()

        while True:
            try:
                choice = input("请输入选择 (Y/N/Q): ").strip().upper()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消")
                sys.exit(0)

            if choice in ("Y", "YES"):
                print(f"\n✅ 继续监控: {prev_nick or prev_url}")
                print()
                return prev_url
            elif choice in ("N", "NO"):
                print("\n🔄 准备切换新主播...")
                print()
                break  # Fall through to new URL prompt
            elif choice in ("Q", "QUIT", "EXIT"):
                print("\n👋 已退出")
                sys.exit(0)
            else:
                print("❌ 无效输入，请输入 Y (继续) / N (切换) / Q (退出)")

    # Prompt for new target URL
    print("请粘贴要监控的主播主页链接:")
    print("示例: https://v.douyin.com/xxxxx/")
    print("       https://www.douyin.com/user/xxxxx")
    print()

    while True:
        try:
            url = input("链接: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)

        if not url:
            print("❌ 链接不能为空，请重新输入")
            continue

        # Clean the input: extract just the URL portion
        # Users often paste raw share text like:
        #   "https://v.douyin.com/xxxxx/ 3@1.com :4pm"
        # where the trailing text is a Douyin copy-paste artifact.
        # Split on whitespace and find the first http(s) token.
        tokens = url.split()
        url = ""
        for token in tokens:
            if token.startswith("http://") or token.startswith("https://"):
                url = token
                break
        if not url:
            # Fallback: take the first token if none starts with http
            url = tokens[0] if tokens else ""

        try:
            url = validate_streamer_url(url)
        except ValueError as exc:
            print(f"⚠️  {exc}，请确认后重新输入")
            continue

        print(f"\n✅ 已设置监控目标: {url}")
        print()
        return url


# ===== Main Monitor =====


class DouyinLiveMonitor:
    """Main monitor that orchestrates detection and notification."""

    def __init__(self, config: Dict[str, Any], target_url: str = "", debug: bool = False):
        self.config = config
        self.target_url = target_url or config.get("target_url", "")
        self.check_interval = config.get("check_interval", 30)
        self.notify_on_end = config.get("notify_on_stream_end", True)

        if not self.target_url:
            logger.error("未设置目标主播链接")
            sys.exit(1)

        # Initialize components
        self.client = DouyinClient(debug=debug)
        self.notifier = _create_notifier(config)

        self.state = MonitorState()
        self.state.repeat_notify_interval = config.get(
            "repeat_notify_interval", 600
        )
        self.state.max_repeat_notifications = config.get(
            "max_repeat_notifications", 3
        )
        self.running = True
        self._stop_event = threading.Event()

    def _resolve_sec_uid(self) -> Optional[str]:
        """Resolve the target user's sec_uid."""
        try:
            logger.info(f"正在解析目标用户: {self.target_url}")
            final_url = self.client.resolve_short_link(self.target_url)
            sec_uid = self.client.extract_sec_uid(final_url)

            if sec_uid:
                logger.info(f"用户 ID: {sec_uid}")
                # Get additional user info
                user_info = self.client.get_user_info(sec_uid)
                if user_info.get("nickname"):
                    self.state.streamer_nickname = user_info["nickname"]
                    logger.info(
                        f"用户昵称: {self.state.streamer_nickname}"
                    )
                return sec_uid
            else:
                logger.error("无法从URL中提取用户ID")
                return None

        except Exception as e:
            logger.error(f"解析用户失败: {e}")
            return None

    def prepare(self) -> Optional[str]:
        """Resolve the target before scheduled checks begin."""
        return self._resolve_sec_uid()

    def _handle_live_detected(self, info: Dict[str, Any]) -> None:
        """Handle when streamer goes live (first detection)."""
        if not self.state.should_notify_first_live():
            return

        nickname = info.get("nickname") or self.state.streamer_nickname
        room_id = info.get("room_id", "")
        title = info.get("title", "")

        line = f"🔴 {nickname} 开播! 房间 {room_id}"
        if title:
            line += f"  {title[:30]}"
        print(line)
        logger.info(f"[LIVE] 检测到开播! 博主: {nickname}, 房间: {room_id}")

        success = self.notifier.send_live_notification(
            nickname=nickname,
            room_id=room_id,
            title=title,
        )
        if success:
            self.state.mark_first_notification_sent()
        elif self.notifier.delivery_unknown:
            # The POST may have been accepted before the connection failed.
            self.state.mark_first_notification_sent()

        if success:
            logger.info("[OK] 开播通知已发送!")
        else:
            logger.error("[FAIL] 开播通知发送失败!")

    def _handle_repeat_notification(self, info: Dict[str, Any]) -> None:
        """Handle periodic repeat notification while streamer is still live."""
        nickname = info.get("nickname") or self.state.streamer_nickname
        room_id = info.get("room_id", "")
        title = info.get("title", "")
        count = self.state.repeat_notify_count
        max_count = self.state.max_repeat_notifications

        logger.info(
            f"[REPEAT] 仍在直播中 ({count}/{max_count}): "
            f"{nickname}, 房间: {room_id}"
        )

        duration_str = self.state._format_duration()

        success = self.notifier.send_repeat_live_notification(
            nickname=nickname,
            room_id=room_id,
            title=title,
            repeat_count=count,
            max_repeat=max_count,
            duration=duration_str,
        )

        if success:
            logger.info(f"[OK] 重复推送已发送 ({count}/{max_count})!")
        else:
            logger.error("[FAIL] 重复推送发送失败!")

    def _check_daily_intimacy_reminder(self, info: Dict[str, Any]) -> None:
        """Check and send daily intimacy reminder at 23:57, 23:58, 23:59.

        Only sends when the streamer is currently live.
        Each minute slot is sent at most once per day.
        """
        if not self.config.get("enable_daily_intimacy_reminder", True):
            return

        now = datetime.now()
        hour = now.hour
        minute = now.minute
        today = now.strftime("%Y-%m-%d")

        # Only trigger at 23:57, 23:58, 23:59
        if hour != 23 or minute not in (57, 58, 59):
            return

        # Reset sent-set when date has changed
        if self.state._daily_reminder_date != today:
            self.state._daily_reminder_minutes_sent.clear()
            self.state._daily_reminder_date = today

        # Already sent for this minute slot today
        if minute in self.state._daily_reminder_minutes_sent:
            return

        # Only send when the streamer is currently live
        if self.state.status != self.state.LIVE:
            logger.debug(
                f"[亲密度提醒] 23:{minute} 但主播不在直播中，跳过"
            )
            return

        nickname = info.get("nickname") or self.state.streamer_nickname
        room_id = info.get("room_id", "")
        title = info.get("title", "")

        print(f"\n⏰ 每日亲密度提醒 (23:{minute}) - {nickname} 仍在直播!")
        logger.info(f"[亲密度提醒] 23:{minute} - {nickname} 亲密度即将刷新")

        success = self.notifier.send_daily_intimacy_reminder(
            nickname=nickname,
            minute=minute,
            room_id=room_id,
            title=title,
        )
        if success:
            self.state._daily_reminder_minutes_sent.add(minute)
        elif self.notifier.delivery_unknown:
            self.state._daily_reminder_minutes_sent.add(minute)

        if success:
            logger.info(f"[OK] 亲密度提醒已发送 (23:{minute})!")
        else:
            logger.error(f"[FAIL] 亲密度提醒发送失败 (23:{minute})!")

    def _handle_stream_end(self) -> None:
        """Handle when streamer goes offline."""
        if not self.notify_on_end:
            return

        nickname = self.state.streamer_nickname
        duration = self.state._format_duration()

        print(f"⚫ {nickname} 已下播 (时长 {duration})")
        logger.info(f"[OFFLINE] 直播结束: {nickname}")

        self.notifier.send_stream_end_notification(
            nickname=nickname, duration=duration
        )

    def check_once(self) -> Dict[str, Any]:
        """Perform a single live status check."""
        result = self.client.check_live(target_url=self.target_url)
        if result.get("indeterminate"):
            reason = result.get("error") or "所有检测方法均无法确认状态"
            raise RuntimeError(f"直播状态暂时无法确认: {reason}")

        # Update state
        is_live = result.get("is_live", False)
        transition = self.state.transition(is_live, result)

        # Handle transitions
        if transition == "went_live":
            self._handle_live_detected(result)
        elif transition == "still_live":
            # Still live — check if we should send repeat notification
            if self.state.should_notify_repeat():
                self._handle_repeat_notification(result)
        elif transition == "went_offline":
            self._handle_stream_end()

        # Always check daily intimacy reminder (23:57/58/59)
        self._check_daily_intimacy_reminder(result)

        return result

    def run(self) -> None:
        """Main monitoring loop."""
        logger.info("=" * 50)
        logger.info("抖音直播监听器 启动")
        logger.info(f"目标: {self.target_url}")
        logger.info(f"检测间隔: {self.check_interval}s")
        logger.info(f"日志文件: {LOG_FILE}")
        logger.info("=" * 50)

        # Resolve the target user
        sec_uid = self._resolve_sec_uid()
        if not sec_uid:
            logger.error("无法解析目标用户，退出")
            sys.exit(1)

        # Save monitor state for next run
        save_monitor_state(self.target_url, self.state.streamer_nickname)

        # Send startup notification (optional test)
        startup_notify = self.config.get("startup_notify", False)
        if startup_notify:
            self.notifier.send(
                title="[START] 抖音直播监听器已启动",
                desp=(
                    f"**监控目标**: {self.state.streamer_nickname}\n"
                    f"**检测间隔**: {self.check_interval}s\n"
                    f"**启动时间**: "
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                ),
            )

        logger.info(f"开始监控: {self.state.get_summary()}")

        self._start_stop_listener()
        print("监控已启动，输入 q 后回车可终止并退出；也可按 Ctrl+C。")

        check_count = 0
        consecutive_errors = 0
        max_consecutive_errors = 10

        while self.running:
            next_check = time.time() + self.check_interval

            try:
                check_count += 1

                result = self.check_once()
                is_live = result.get("is_live", False)
                method = result.get("method", "unknown")

                # --- compact one-line status to terminal + full log ---
                nick = self.state.streamer_nickname or "?"
                now = datetime.now().strftime("%m-%d %H:%M")
                if is_live:
                    title = (result.get("title") or "")[:30]
                    line = f"🔴 {now}  #{check_count}  {nick} 直播中 | {method}"
                    if title:
                        line += f" | {title}"
                    logger.info(line)
                    print(line)
                else:
                    line = f"⚫ {now}  #{check_count}  {nick} 未开播 | {method}"
                    logger.info(line)
                    if check_count % 10 == 1:
                        print(line)

                consecutive_errors = 0  # Reset error counter on success

            except KeyboardInterrupt:
                logger.info("\n收到中断信号，正在退出...")
                break
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    f"检测异常 (连续错误 {consecutive_errors}/"
                    f"{max_consecutive_errors}): {e}",
                    exc_info=True,
                )
                # Errors always visible on terminal (WARNING+)
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"连续错误达到 {max_consecutive_errors} 次，退出"
                    )
                    break

            # Wait until the next scheduled check (compensates for check duration)
            if self.running:
                sleep_time = next_check - time.time()
                if sleep_time > 0:
                    try:
                        self._stop_event.wait(sleep_time)
                    except KeyboardInterrupt:
                        break

        logger.info("监听器已停止")

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self.running = False
        stop_event = getattr(self, "_stop_event", None)
        if stop_event is not None:
            stop_event.set()

    def _start_stop_listener(self) -> None:
        """Listen for a console command that stops continuous monitoring."""
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not hasattr(stdin, "isatty") or not stdin.isatty():
            return

        def listen() -> None:
            while self.running:
                try:
                    command = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return
                if command in {"q", "quit", "exit", "stop", "退出"}:
                    print("正在停止监控...")
                    self.stop()
                    return
                if command:
                    print("未知命令；输入 q 后回车退出。")

        threading.Thread(
            target=listen,
            name="monitor-stop-listener",
            daemon=True,
        ).start()


# ===== CLI Entry Point =====


def _load_streamer_entries(
    config_path: Path, config: Dict[str, Any]
) -> list[Dict[str, Any]]:
    """Validate v1.2 config and import the legacy single target once."""
    legacy_state = load_monitor_state()
    entries, normalized = normalize_streamers(config)
    changed = normalized
    migrated = False
    if not entries and legacy_state.get("target_url"):
        try:
            migrated = migrate_legacy_streamer(config, legacy_state)
            entries, normalized = normalize_streamers(config)
            changed = changed or migrated or normalized
        except ValueError as exc:
            logger.warning(f"旧版主播记录无效，已跳过迁移: {exc}")
    if changed:
        save_config_atomic(config_path, config)
    if migrated:
        logger.info("已将上次监控的主播迁移到多主播配置")
    return entries


def _print_streamers(entries: list[Dict[str, Any]]) -> None:
    """Print the configured streamer list without exposing push secrets."""
    if not entries:
        print("尚未配置主播。")
        return
    print(f"已配置 {len(entries)} 个主播:")
    for entry in entries:
        status = "启用" if entry.get("enabled", True) else "停用"
        label = entry.get("label") or "未命名"
        print(f"  [{status}] {entry['id']}  {label}  {entry['url']}")


def _confirm_remove(entry: Dict[str, Any], assume_yes: bool) -> bool:
    """Require confirmation before deleting a configured streamer."""
    if assume_yes:
        return True
    stdin = getattr(sys, "stdin", None)
    if stdin is None or not hasattr(stdin, "isatty") or not stdin.isatty():
        raise ValueError("非交互环境删除主播时必须同时使用 --yes")
    label = entry.get("label") or entry["url"]
    try:
        answer = input(
            f"确认删除主播 {label} ({entry['id']})？[y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt) as exc:
        raise ValueError("无法读取确认输入，请同时使用 --yes") from exc
    return answer in {"y", "yes"}


def _handle_streamer_command(
    args: argparse.Namespace,
    config_path: Path,
    config: Dict[str, Any],
    entries: list[Dict[str, Any]],
) -> bool:
    """Handle one configuration-only CLI command and report completion."""
    if args.list_streamers:
        _print_streamers(entries)
        return True

    if args.add_streamer:
        entry = add_streamer(config, args.add_streamer, label=args.label or "")
        save_config_atomic(config_path, config)
        print(f"已添加主播: {entry['id']}  {entry['url']}")
        return True

    if args.remove_streamer:
        entry = next(
            (
                item
                for item in entries
                if item["id"] == args.remove_streamer.strip()
            ),
            None,
        )
        if entry is None:
            raise ValueError(f"未找到主播 ID: {args.remove_streamer}")
        if not _confirm_remove(entry, args.yes):
            print("已取消删除。")
            return True
        removed = remove_streamer(config, args.remove_streamer)
        save_config_atomic(config_path, config)
        print(f"已删除主播: {removed['id']}  {removed['url']}")
        return True

    if args.enable_streamer:
        entry = set_streamer_enabled(config, args.enable_streamer, True)
        save_config_atomic(config_path, config)
        print(f"已启用主播: {entry['id']}  {entry['url']}")
        return True

    if args.disable_streamer:
        entry = set_streamer_enabled(config, args.disable_streamer, False)
        save_config_atomic(config_path, config)
        print(f"已停用主播: {entry['id']}  {entry['url']}")
        return True

    return False


def _safe_print(value: str) -> None:
    """Print safely on legacy Windows consoles."""
    try:
        print(value)
    except UnicodeEncodeError:
        print(value.encode("ascii", errors="replace").decode("ascii"))


def _print_once_results(
    entries: list[Dict[str, Any]], results: Dict[str, Dict[str, Any]]
) -> None:
    """Print one detailed result block per successfully checked streamer."""
    by_id = {entry["id"]: entry for entry in entries}
    for streamer_id, result in results.items():
        entry = by_id.get(streamer_id, {})
        _safe_print("\n" + "=" * 48)
        _safe_print(f"主播 ID: {streamer_id}")
        _safe_print(
            f"  博主: {result.get('nickname') or entry.get('label') or '未知'}"
        )
        _safe_print(
            f"  直播中: {'是 [LIVE]' if result.get('is_live') else '否 [OFFLINE]'}"
        )
        _safe_print(f"  房间 ID: {result.get('room_id', 'N/A')}")
        _safe_print(f"  标题: {result.get('title', 'N/A')}")
        _safe_print(f"  检测方法: {result.get('method', 'N/A')}")
        _safe_print(f"  尝试方法: {result.get('methods_tried', 'N/A')}")
    if results:
        _safe_print("=" * 48)


def main():
    parser = argparse.ArgumentParser(
        description="抖音直播监听器 - 检测开播并通过Server酱³推送通知",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python monitor.py                    # 持续监控 (使用 config.json)
  python monitor.py --once             # 单次检测
  python monitor.py --config my.json   # 使用自定义配置
  python monitor.py --test             # 测试 Server酱³ 连接
  python monitor.py --list-streamers   # 查看主播列表
  python monitor.py --add-streamer URL # 添加主播
        """,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"douyin-live-monitor v{APP_VERSION}",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_CONFIG),
        help=f"配置文件路径 (默认: {DEFAULT_CONFIG})",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--once",
        action="store_true",
        help="对全部已启用主播执行一次检测后退出",
    )
    operation.add_argument(
        "--test",
        action="store_true",
        help="测试 Server酱³ 连接后退出",
    )
    operation.add_argument(
        "--list-streamers",
        action="store_true",
        help="列出全部主播后退出",
    )
    operation.add_argument(
        "--add-streamer",
        metavar="URL",
        help="添加一个主播后退出",
    )
    operation.add_argument(
        "--remove-streamer",
        metavar="ID",
        help="按完整 ID 删除一个主播后退出",
    )
    operation.add_argument(
        "--enable-streamer",
        metavar="ID",
        help="按完整 ID 启用一个主播后退出",
    )
    operation.add_argument(
        "--disable-streamer",
        metavar="ID",
        help="按完整 ID 停用一个主播后退出",
    )
    parser.add_argument(
        "--label",
        help="配合 --add-streamer 保存显示名称",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="确认非交互式删除操作",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="调试模式 - 保存原始API响应到 debug_dumps/ 目录",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="静默模式 (仅错误输出)",
    )

    args = parser.parse_args()
    if args.label and not args.add_streamer:
        parser.error("--label 只能与 --add-streamer 一起使用")
    if args.yes and not args.remove_streamer:
        parser.error("--yes 只能与 --remove-streamer 一起使用")

    # Reconfigure console level for --verbose / --quiet
    if args.verbose:
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)
    elif args.quiet:
        for h in logging.getLogger().handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.ERROR)

    config_path = Path(args.config)
    try:
        config = load_config(config_path)
        entries = _load_streamer_entries(config_path, config)
        if _handle_streamer_command(args, config_path, config, entries):
            return
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.error(f"配置处理失败: {exc}")
        _pause_if_frozen()
        sys.exit(2)

    if args.test:
        if not is_serverchan_configured(config):
            logger.error(
                "Server酱³ 尚未配置!\n"
                "请先运行 python monitor.py 进行配置，"
                "或在 config.json 中手动填入推送 URL。\n"
                "获取方式: 访问 https://sc3.ft07.com 微信扫码登录"
            )
            _pause_if_frozen()
            sys.exit(1)
        notifier = _create_notifier(config)
        logger.info("测试 Server酱³ 连接...")
        if notifier.verify_connection():
            logger.info("[OK] Server酱³ 连接测试成功!")
        else:
            logger.error("[FAIL] Server酱³ 连接测试失败!")
        return

    if not is_serverchan_configured(config):
        config = prompt_serverchan_config(config_path, config)

    if not entries:
        target_url = prompt_target_url()
        entry = add_streamer(config, target_url)
        save_config_atomic(config_path, config)
        entries = list(config["streamers"])
        print(f"已保存主播: {entry['id']}  {entry['url']}")

    active_entries = enabled_streamers(entries)
    if not active_entries:
        logger.error(
            "没有已启用的主播，请使用 --enable-streamer ID 启用任务"
        )
        _pause_if_frozen()
        sys.exit(1)

    def worker_factory(entry: Dict[str, Any]) -> DouyinLiveMonitor:
        return DouyinLiveMonitor(
            dict(config),
            target_url=entry["url"],
            debug=args.debug,
        )

    try:
        service = MonitorService(
            active_entries,
            worker_factory=worker_factory,
            check_interval=config.get("check_interval", 30),
            max_concurrent_checks=config.get("max_concurrent_checks", 2),
            startup_notify=config.get("startup_notify", False),
        )
    except (TypeError, ValueError) as exc:
        logger.error(f"监控参数无效: {exc}")
        _pause_if_frozen()
        sys.exit(2)

    def signal_handler(sig, frame):
        logger.info("\n收到退出信号...")
        service.stop()

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.once:
            logger.info("对全部已启用主播执行单次检测...")
            results = service.check_all_once()
            _print_once_results(active_entries, results)
            if not results:
                sys.exit(1)
        else:
            if not service.run():
                sys.exit(1)
    except KeyboardInterrupt:
        service.stop()
        logger.info("用户中断")
    except Exception as e:
        service.stop()
        logger.error(f"程序异常: {e}", exc_info=True)
        _pause_if_frozen()
        sys.exit(1)

    # Normal exit - pause for frozen exe so user can see output
    _pause_if_frozen()


if __name__ == "__main__":
    main()
