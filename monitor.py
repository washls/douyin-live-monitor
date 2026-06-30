#!/usr/bin/env python3
"""
Douyin Live Monitor with Server酱³ Push Notifications

Monitors a Douyin blogger's live status and sends push notifications
via Server酱³ when they go live.

Usage:
    python monitor.py                  # Run with config.json
    python monitor.py --once           # Single check
    python monitor.py --config my.json # Custom config
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from douyin_client import DouyinClient
from notifier import ServerChanNotifier

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
BASE_DIR = _get_runtime_dir()
DEFAULT_CONFIG = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "monitor.log"
STATE_FILE = BASE_DIR / ".monitor_state.json"

# ===== Logging Setup =====


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure logging to both console and file."""
    logger = logging.getLogger("douyin_monitor")
    logger.setLevel(level)

    # Console handler - wrap stdout for Windows GBK compatibility
    try:
        console_out = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    except (AttributeError, OSError):
        console_out = sys.stdout
    console = logging.StreamHandler(console_out)
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    # File handler
    file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(console)
    logger.addHandler(file_handler)

    # Also configure sub-module loggers
    for name in ["douyin_client", "notifier"]:
        sub_logger = logging.getLogger(name)
        sub_logger.setLevel(level)
        sub_logger.addHandler(console)
        sub_logger.addHandler(file_handler)

    return logger


logger = setup_logging()

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

    def transition(self, is_live: bool, info: Dict[str, Any]) -> str:
        """
        Update state based on current live status.
        Returns 'went_live', 'went_offline', 'still_live', or 'no_change'.
        """
        nickname = info.get("nickname", self.streamer_nickname)
        room_id = info.get("room_id", "")
        title = info.get("title", "")

        if is_live:
            if nickname:
                self.streamer_nickname = nickname
            if room_id:
                self.stream_room_id = room_id
            if title:
                self.stream_title = title

            if self.status == self.LIVE:
                # Still live, same room
                # But reset if stream info changed (new room = new stream)
                if room_id and room_id != self.stream_room_id:
                    logger.info("检测到新直播间 (可能是新一轮直播)")
                    self.stream_room_id = room_id
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
            duration = ""
            if self.last_live_start:
                secs = int(time.time() - self.last_live_start)
                hours, remainder = divmod(secs, 3600)
                mins, secs = divmod(remainder, 60)
                duration = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

            logger.info(
                f"状态变更: LIVE -> OFFLINE (直播时长: {duration})"
            )
            self.status = self.OFFLINE
            self.last_status_change = time.time()
            return "went_offline"

    def should_notify_first_live(self) -> bool:
        """Check if we should send the FIRST live notification.

        Always sends on first detection; no cooldown.
        """
        if self.notification_sent:
            return False
        self.notification_sent = True
        self.last_repeat_notify_time = time.time()
        return True

    def should_notify_repeat(self) -> bool:
        """Check if we should send a REPEAT notification while still live.

        Returns True every `repeat_notify_interval` seconds,
        up to `max_repeat_notifications` times.
        """
        if self.status != self.LIVE:
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
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
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
        "retry_times": 3,
        "retry_delay": 5,
    }


def is_serverchan_configured(config: Dict[str, Any]) -> bool:
    """Check if Server酱 is properly configured."""
    # Check if push_url is set
    if config.get("push_url") and config["push_url"] != "YOUR_PUSH_URL_HERE":
        return True
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

        # Validate URL format
        if "push.ft07.com" in url:
            config["push_url"] = url
            config["sendkey"] = ""  # Will be parsed from URL
            config["push_uid"] = ""  # Will be parsed from URL

            # Save config
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=4)
                print()
                print("✅ Server酱³ 配置已保存!")
                print()
            except OSError as e:
                logger.warning(f"保存配置失败: {e}")
            break
        else:
            print("❌ URL 格式不正确，请粘贴完整的推送 URL")
            print("   格式示例: https://12345.push.ft07.com/send/SCTxxxxxx.send")
            print()

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
    print("         抖音直播监听器")
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

        # Basic URL validation
        if not (url.startswith("http://") or url.startswith("https://")):
            print("❌ 请输入有效的网址 (以 http:// 或 https:// 开头)")
            continue

        if "douyin.com" not in url and "iesdouyin.com" not in url:
            print("⚠️  该链接似乎不是抖音链接，请确认后重新输入")
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
        self.debug = debug

        if not self.target_url:
            logger.error("未设置目标主播链接")
            sys.exit(1)

        # Initialize components
        self.client = DouyinClient(debug=debug)
        self.notifier = ServerChanNotifier(
            sendkey=config.get("sendkey", ""),
            uid=config.get("push_uid"),
            push_url=config.get("push_url"),
            retry_times=config.get("retry_times", 3),
            retry_delay=config.get("retry_delay", 5),
        )

        self.state = MonitorState()
        self.running = True

    def _resolve_sec_uid(self) -> Optional[str]:
        """Resolve the target user's sec_uid."""
        try:
            logger.info(f"正在解析目标用户: {self.target_url}")
            final_url = self.client.resolve_short_link(self.target_url)
            sec_uid = self.client._extract_sec_uid(final_url)

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

    def _handle_live_detected(self, info: Dict[str, Any]) -> None:
        """Handle when streamer goes live (first detection)."""
        if not self.state.should_notify_first_live():
            return

        nickname = info.get("nickname") or self.state.streamer_nickname
        room_id = info.get("room_id", "")
        title = info.get("title", "")

        logger.info(f"[LIVE] 检测到开播! 博主: {nickname}, 房间: {room_id}")

        success = self.notifier.send_live_notification(
            nickname=nickname,
            room_id=room_id,
            title=title,
        )

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

        # Calculate how long they've been live
        duration_str = ""
        if self.state.last_live_start:
            secs = int(time.time() - self.state.last_live_start)
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            parts = []
            if h:
                parts.append(f"{h}小时")
            if m:
                parts.append(f"{m}分钟")
            parts.append(f"{s}秒")
            duration_str = "".join(parts)

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

    def _handle_stream_end(self) -> None:
        """Handle when streamer goes offline."""
        if not self.notify_on_end:
            return

        nickname = self.state.streamer_nickname
        duration = ""
        if self.state.last_live_start:
            secs = int(time.time() - self.state.last_live_start)
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            parts = []
            if h:
                parts.append(f"{h}小时")
            if m:
                parts.append(f"{m}分钟")
            parts.append(f"{s}秒")
            duration = "".join(parts)

        logger.info(f"[OFFLINE] 直播结束: {nickname}")

        self.notifier.send_stream_end_notification(
            nickname=nickname, duration=duration
        )

    def check_once(self) -> Dict[str, Any]:
        """Perform a single live status check."""
        result = self.client.check_live(target_url=self.target_url)

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

        check_count = 0
        consecutive_errors = 0
        max_consecutive_errors = 10

        while self.running:
            try:
                check_count += 1
                logger.debug(
                    f"--- 第 {check_count} 次检测 "
                    f"({datetime.now().strftime('%H:%M:%S')}) ---"
                )

                result = self.check_once()
                is_live = result.get("is_live", False)
                method = result.get("method", "unknown")

                logger.info(
                    f"{self.state.get_summary()} "
                    f"[方法: {method}] "
                    f"[第{check_count}次]"
                )

                if is_live and result.get("title"):
                    logger.info(f"  直播标题: {result['title']}")

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
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"连续错误达到 {max_consecutive_errors} 次，退出"
                    )
                    break

            # Wait before next check
            if self.running:
                try:
                    time.sleep(self.check_interval)
                except KeyboardInterrupt:
                    break

        logger.info("监听器已停止")

    def stop(self) -> None:
        """Signal the monitor to stop."""
        self.running = False


# ===== CLI Entry Point =====


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
        """,
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_CONFIG),
        help=f"配置文件路径 (默认: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行一次检测后退出",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试 Server酱³ 连接后退出",
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

    # Set log level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        for name in ["douyin_monitor", "douyin_client", "notifier"]:
            logging.getLogger(name).setLevel(logging.DEBUG)
    elif args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
        for name in ["douyin_monitor", "douyin_client", "notifier"]:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Load config
    config_path = Path(args.config)
    config = load_config(config_path)

    # First-launch: prompt for Server酱 config if not set
    if not args.test and not is_serverchan_configured(config):
        config = prompt_serverchan_config(config_path, config)

    if args.test:
        # Check if Server酱 is configured before testing
        if not is_serverchan_configured(config):
            logger.error(
                "Server酱³ 尚未配置!\n"
                "请先运行 python monitor.py 进行配置，"
                "或在 config.json 中手动填入推送 URL。\n"
                "获取方式: 访问 https://sc3.ft07.com 微信扫码登录"
            )
            # When running as frozen exe, pause so user can see the error
            if getattr(sys, 'frozen', False):
                try:
                    input("按 Enter 键退出...")
                except (EOFError, KeyboardInterrupt):
                    pass
            sys.exit(1)

        # Test Server酱³ connection (no target URL needed)
        notifier = ServerChanNotifier(
            sendkey=config.get("sendkey", ""),
            uid=config.get("push_uid"),
            push_url=config.get("push_url"),
            retry_times=config.get("retry_times", 3),
            retry_delay=config.get("retry_delay", 5),
        )

        logger.info("测试 Server酱³ 连接...")
        if notifier.verify_connection():
            logger.info("[OK] Server酱³ 连接测试成功!")
        else:
            logger.error("[FAIL] Server酱³ 连接测试失败!")
        return

    # Prompt for target URL (interactive)
    target_url = prompt_target_url()

    # Create monitor (pass debug flag if set)
    monitor = DouyinLiveMonitor(config, target_url=target_url, debug=getattr(args, 'debug', False))

    # Handle signals for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("\n收到退出信号...")
        monitor.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.once:
            # Single check mode
            logger.info("执行单次检测...")
            sec_uid = monitor._resolve_sec_uid()
            if not sec_uid:
                logger.error("无法解析目标用户")
                sys.exit(1)

            result = monitor.check_once()

            # Use safe print for Windows GBK consoles
            def safe_print(s):
                try:
                    print(s)
                except UnicodeEncodeError:
                    print(s.encode('ascii', errors='replace').decode('ascii'))

            safe_print("\n" + "=" * 40)
            safe_print("检测结果:")
            safe_print(f"  博主: {result.get('nickname', '未知')}")
            safe_print(f"  直播中: {'是 [LIVE]' if result.get('is_live') else '否 [OFFLINE]'}")
            safe_print(f"  房间ID: {result.get('room_id', 'N/A')}")
            safe_print(f"  标题: {result.get('title', 'N/A')}")
            safe_print(f"  检测方法: {result.get('method', 'N/A')}")
            safe_print(f"  尝试方法: {result.get('methods_tried', 'N/A')}")
            safe_print("=" * 40)
        else:
            # Continuous monitoring
            monitor.run()

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"程序异常: {e}", exc_info=True)
        # When running as frozen exe, pause so user can see the error
        if getattr(sys, 'frozen', False):
            print()
            try:
                input("按 Enter 键退出...")
            except (EOFError, KeyboardInterrupt):
                pass
        sys.exit(1)

    # Normal exit - pause for frozen exe so user can see output
    if getattr(sys, 'frozen', False):
        try:
            input("按 Enter 键退出...")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    # Fix Windows GBK console encoding
    if sys.platform == 'win32':
        try:
            sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
        except (AttributeError, OSError):
            pass
    main()
