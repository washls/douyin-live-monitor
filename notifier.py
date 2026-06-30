"""
Server酱³ (ServerChan3) Notification Client

Sends push notifications to mobile devices via Server酱³ API.
API docs: https://doc2.ft07.com/zh/serverchan3
"""

import json
import logging
import time
from typing import Dict, Any, Optional

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
            retry_times: Max retry count for failed sends
            retry_delay: Delay between retries in seconds
        """
        self.retry_times = retry_times
        self.retry_delay = retry_delay

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

        logger.info(f"Server酱³ 通知器已初始化 (UID: {self.uid})")

    @staticmethod
    def _parse_push_url(url: str) -> dict:
        """
        Parse a full Server酱³ push URL to extract UID and SendKey.

        Expected format: https://<uid>.push.ft07.com/send/<sendkey>.send

        Returns:
            dict with 'uid' and 'sendkey' keys.
        """
        import re
        # Match: https://<uid>.push.ft07.com/send/<sendkey>.send
        match = re.match(
            r'https?://(\d+)\.push\.ft07\.com/send/(\w+)\.send',
            url
        )
        if match:
            return {"uid": match.group(1), "sendkey": match.group(2)}
        # Fallback: try to extract whatever we can
        logger.warning(f"无法解析推送 URL 格式: {url}")
        return {"uid": "", "sendkey": ""}

    def set_api_url(self, url: str) -> None:
        """Override the API URL directly (for custom endpoints)."""
        self.api_url = url
        logger.info(f"API URL 已设置为: {url}")

    def send(
        self,
        title: str,
        desp: str = "",
        tags: Optional[str] = None,
        short: Optional[str] = None,
    ) -> bool:
        """
        Send a notification via Server酱³.

        Args:
            title: Notification title (required)
            desp: Notification body, supports Markdown
            tags: Optional tags, separated by |
            short: Optional short description

        Returns:
            True if sent successfully, False otherwise
        """
        # Build payload
        payload: Dict[str, str] = {
            "title": title,
        }
        if desp:
            payload["desp"] = desp
        if tags:
            payload["tags"] = tags
        if short:
            payload["short"] = short

        # Retry loop
        for attempt in range(1, self.retry_times + 1):
            try:
                logger.info(
                    f"发送通知 (尝试 {attempt}/{self.retry_times}): "
                    f"title='{title[:50]}'"
                )

                # Try POST with JSON body first
                resp = requests.post(
                    self.api_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json;charset=utf-8"
                    },
                    timeout=15,
                )

                # Fall back to GET if POST fails
                if resp.status_code >= 400:
                    logger.debug(
                        f"POST 失败 ({resp.status_code}), 尝试 GET..."
                    )
                    resp = requests.get(
                        self.api_url,
                        params=payload,
                        timeout=15,
                    )

                resp.raise_for_status()

                result = resp.json()
                code = result.get("code", -1)

                if code == 0:
                    logger.info("通知发送成功!")
                    return True
                else:
                    msg = result.get("message", "Unknown error")
                    logger.warning(
                        f"Server酱³ 返回错误: code={code}, "
                        f"message={msg}"
                    )

                    # Don't retry on client errors (invalid SendKey, etc.)
                    if 100 <= code < 200:
                        logger.error(
                            f"客户端错误 (code={code})，不重试。"
                            f"请检查 SendKey 和 UID 是否正确。"
                        )
                        return False

            except requests.Timeout:
                logger.warning(
                    f"通知发送超时 (尝试 {attempt}/{self.retry_times})"
                )
            except requests.ConnectionError as e:
                logger.warning(
                    f"连接失败 (尝试 {attempt}/{self.retry_times}): {e}"
                )
            except Exception as e:
                logger.warning(
                    f"通知发送异常 (尝试 {attempt}/{self.retry_times}): {e}"
                )

            # Wait before retry
            if attempt < self.retry_times:
                logger.debug(f"等待 {self.retry_delay}s 后重试...")
                time.sleep(self.retry_delay)

        logger.error(f"通知发送失败，已达最大重试次数 ({self.retry_times})")
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
