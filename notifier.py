"""
Server酱³ (ServerChan3) Notification Client

Sends push notifications to mobile devices via Server酱³ API.
API docs: https://doc2.ft07.com/zh/serverchan3
"""

import logging
import re
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Server酱³ API base URL template
# Format: https://<uid>.push.ft07.com/send/<sendkey>.send
SERVERCHAN_URL_TEMPLATE = "https://{uid}.push.ft07.com/send/{sendkey}.send"

# Retry settings
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


class ServerChanNotifier:
    """Server酱³ notification client."""

    def __init__(
        self,
        sendkey: str = "",
        uid: Optional[str] = None,
        push_url: Optional[str] = None,
        retry_times: int = MAX_RETRIES,
        retry_delay: int = RETRY_DELAY,
    ):
        """
        Initialize the notifier.

        Args:
            sendkey: Server酱³ SendKey
            uid: User ID (parsed from push_url if not provided)
            push_url: Full Server酱³ push URL (e.g. https://<uid>.push.ft07.com/send/<sendkey>.send)
            retry_times: Deprecated compatibility option; POST is not replayed.
            retry_delay: Deprecated compatibility option; POST is not replayed.
        """
        # Retained in the constructor for compatibility with older callers.
        # Notification POSTs are intentionally never replayed because a
        # timeout can occur after the server has already accepted the message.
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        # A transport failure after POST is ambiguous: the server may already
        # have accepted the notification. Callers use this flag to suppress
        # the same event on the next polling round.
        self.delivery_unknown = False

        # If a full push_url is provided, parse sendkey and uid from it
        if push_url:
            parsed = self._parse_push_url(push_url)
            self.sendkey = parsed["sendkey"]
            self.uid = parsed["uid"]
            self.api_url = push_url
        else:
            self.sendkey = sendkey
            if uid:
                self.uid = uid
            else:
                self.uid = ""
            self.api_url = SERVERCHAN_URL_TEMPLATE.format(
                uid=self.uid, sendkey=self.sendkey
            )

        logger.debug(f"Server酱³ 通知器已初始化")

    @staticmethod
    def _parse_push_url(url: str) -> Dict[str, str]:
        """
        Parse a full Server酱³ push URL to extract UID and SendKey.

        Expected format: https://<uid>.push.ft07.com/send/<sendkey>.send

        Returns:
            dict with 'uid' and 'sendkey' keys.

        Raises:
            ValueError: if the URL format is not recognized.
        """
        # Match: https://<uid>.push.ft07.com/send/<sendkey>.send
        # UID is typically numeric but may change; accept alphanumeric + hyphens
        match = re.match(
            r'https?://([\w-]+)\.push\.ft07\.com/send/([\w-]+)\.send',
            url
        )
        if match:
            return {"uid": match.group(1), "sendkey": match.group(2)}
        raise ValueError(f"无法解析推送 URL 格式: {url}")

    def set_api_url(self, url: str) -> None:
        """Override the API URL without exposing credentials in logs."""
        self.api_url = url
        safe_url = re.sub(r"(/send/)[^/?]+", r"\1<redacted>", url)
        logger.info(f"API URL 已设置为: {safe_url}")

    def send(self, title: str, desp: str = "", tags: Optional[str] = None,
             short: Optional[str] = None) -> bool:
        """Send one notification; never replay an ambiguous POST."""
        self.delivery_unknown = False
        payload: Dict[str, str] = {"title": title}
        if desp:
            payload["desp"] = desp
        if tags:
            payload["tags"] = tags
        if short:
            payload["short"] = short

        try:
            logger.info(f"发送通知 (单次尝试): title='{title[:50]}'")
            # Use a one-shot request for notifications. A shared keep-alive
            # connection can become stale during the 10-minute idle interval;
            # requests.post creates and closes a short-lived session here.
            resp = requests.post(
                self.api_url,
                json=payload,
                headers={"Content-Type": "application/json;charset=utf-8"},
                timeout=15,
            )
            resp.raise_for_status()
            try:
                result = resp.json()
            except ValueError:
                logger.error("ServerChan 返回了非 JSON 响应，不重试")
                return False
            if result.get("code", -1) == 0:
                logger.info(f"通知发送成功: {result.get('message', 'N/A')}")
                return True
            logger.warning(
                f"ServerChan 返回错误: code={result.get('code', -1)}, "
                f"message={result.get('message', 'Unknown error')}"
            )
            return False
        except requests.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else 0
            if status_code >= 500:
                self.delivery_unknown = True
            logger.warning(f"HTTP 错误 ({status_code})，不重试: {e}")
        except (requests.Timeout, requests.ConnectionError) as e:
            self.delivery_unknown = True
            logger.warning(f"通知传输结果未知，不重试: {e}")
        except Exception as e:
            self.delivery_unknown = True
            logger.warning(f"通知发送异常，不重试: {e}")
        return False

    def send_live_notification(
        self, nickname: str, room_id: str, title: str = ""
    ) -> bool:
        """
        Send a formatted "streamer went live" notification.

        Args:
            nickname: Streamer nickname
            room_id: Live room ID
            title: Live stream title (optional)

        Returns:
            True if sent successfully
        """
        live_url = f"https://live.douyin.com/{room_id}" if room_id else ""

        # Build Markdown message
        md_parts = [
            "📺 **开播提醒**",
            "",
            f"- **博主**: {nickname}",
        ]
        if title:
            md_parts.append(f"- **标题**: {title}")
        if room_id:
            md_parts.append(f"- **房间ID**: `{room_id}`")
        if live_url:
            md_parts.append(f"- **链接**: [点击观看]({live_url})")

        md_parts.extend([
            "",
            f"⏰ 检测时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ])

        desp = "\n".join(md_parts)

        notify_title = f"🔴 {nickname} 正在直播!"
        if title:
            notify_title += f" - {title[:30]}"

        return self.send(
            title=notify_title,
            desp=desp,
            tags="抖音直播|开播提醒",
            short=f"{nickname} 开播了",
        )

    def send_stream_end_notification(
        self, nickname: str, duration: str = ""
    ) -> bool:
        """
        Send a "stream ended" notification.

        Args:
            nickname: Streamer nickname
            duration: Stream duration (optional)

        Returns:
            True if sent successfully
        """
        md_parts = [
            "⏹️ **下播提醒**",
            "",
            f"- **博主**: {nickname}",
            f"- **时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        if duration:
            md_parts.append(f"- **直播时长**: {duration}")

        desp = "\n".join(md_parts)

        return self.send(
            title=f"⏹️ {nickname} 已下播",
            desp=desp,
            tags="抖音直播|下播提醒",
            short=f"{nickname} 已下播",
        )

    def send_repeat_live_notification(
        self,
        nickname: str,
        room_id: str = "",
        title: str = "",
        repeat_count: int = 1,
        max_repeat: int = 3,
        duration: str = "",
    ) -> bool:
        """
        Send a periodic "still live" reminder notification.

        Args:
            nickname: Streamer nickname
            room_id: Live room ID
            title: Live stream title
            repeat_count: Current repeat number (1-based)
            max_repeat: Maximum repeat notifications
            duration: How long they've been live

        Returns:
            True if sent successfully
        """
        live_url = f"https://live.douyin.com/{room_id}" if room_id else ""

        md_parts = [
            f"🔴 **仍在直播提醒 ({repeat_count}/{max_repeat})**",
            "",
            f"- **博主**: {nickname}",
        ]
        if title:
            md_parts.append(f"- **标题**: {title}")
        if room_id:
            md_parts.append(f"- **房间ID**: `{room_id}`")
        if live_url:
            md_parts.append(f"- **链接**: [点击观看]({live_url})")
        if duration:
            md_parts.append(f"- **已播时长**: {duration}")

        md_parts.extend([
            "",
            f"⏰ 检测时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"📢 这是第 {repeat_count}/{max_repeat} 次重复提醒",
        ])

        desp = "\n".join(md_parts)

        notify_title = f"🔴 {nickname} 仍在直播 ({repeat_count}/{max_repeat})"
        if title:
            notify_title += f" - {title[:20]}"

        return self.send(
            title=notify_title,
            desp=desp,
            tags="抖音直播|持续提醒",
            short=f"{nickname} 仍在直播 ({repeat_count}/{max_repeat})",
        )

    def send_daily_intimacy_reminder(
        self,
        nickname: str,
        minute: int,
        room_id: str = "",
        title: str = "",
    ) -> bool:
        """
        Send a daily intimacy refresh reminder at 23:57 / 23:58 / 23:59.

        Args:
            nickname: Streamer nickname
            minute: Which minute slot (57, 58, or 59)
            room_id: Live room ID
            title: Live stream title

        Returns:
            True if sent successfully
        """
        live_url = f"https://live.douyin.com/{room_id}" if room_id else ""
        remaining = 60 - minute  # minutes until midnight reset

        md_parts = [
            "⏰ **每日亲密度刷新提醒**",
            "",
            f"🔥 **{nickname}** 正在直播中！",
            "",
            "---",
            "",
            f"⏳ 距离今日亲密度刷新还剩 **{remaining} 分钟**",
            "",
            "> 每天 00:00 亲密度会重置，",
            "> 今天的亲密度额度还未用满的话，",
            "> 抓紧最后几分钟送送礼、点点赞吧 ~ 💝",
            "",
            "---",
            "",
        ]
        if title:
            md_parts.append(f"📺 **直播标题**: {title}")
        if room_id:
            md_parts.append(f"🏠 **房间ID**: `{room_id}`")
        if live_url:
            md_parts.append(f"🔗 **链接**: [点击观看]({live_url})")

        md_parts.extend([
            "",
            f"🕐 提醒时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "💡 亲密度小贴士: 观看、点赞、送礼、分享直播间都可以增加亲密度哦~",
        ])

        desp = "\n".join(md_parts)

        notify_title = f"⏰ {nickname} 还在直播! 亲密度还剩{remaining}分钟刷新"
        minute_label = f"23:{minute}"

        return self.send(
            title=notify_title,
            desp=desp,
            tags="抖音直播|亲密度提醒",
            short=f"⏰{minute_label} 亲密度即将刷新 - {nickname} 直播中",
        )

    def verify_connection(self) -> bool:
        """
        Verify the Server酱³ connection by sending a test message.

        Returns:
            True if the test was successful
        """
        return self.send(
            title="🧪 抖音直播监听器 - 连接测试",
            desp=(
                "**抖音直播监听器** 已成功连接到 Server酱³!\n\n"
                f"- 测试时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"- 状态: ✅ 连接正常\n"
            ),
            tags="系统测试",
        )
