"""
Douyin Live Status Client

Detects whether a Douyin user is currently live streaming.
Uses multiple detection methods with automatic fallback.

Detection methods (tried in order):
  1. HTML page parsing (douyin.com user profile)
  2. IES share page parsing (iesdouyin.com/share/user/)
  3. IES API v2 (iesdouyin.com/web/api/v2/user/info/)
  4. Douyin API (douyin.com/aweme/v1/web/user/profile/other/)
  5. Webcast room check (live.douyin.com/webcast/)
  6. Live room page direct access
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any
from urllib.parse import unquote, urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

# Default browser-like headers
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ===== Path Helpers (supports PyInstaller frozen exe) =====

def _get_bundle_dir() -> str:
    """Get the directory containing bundled assets (x-bogus.js, etc.).

    When frozen by PyInstaller, assets are in sys._MEIPASS.
    When running as script, it's the script's directory.
    """
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def _get_runtime_dir() -> str:
    """Get the directory for runtime writable files (debug_dumps, etc.).

    When frozen, use the directory next to the .exe.
    When running as script, use the script's directory.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Base directories
BUNDLE_DIR = _get_bundle_dir()       # Read-only bundled assets
RUNTIME_DIR = _get_runtime_dir()     # Writable runtime files

X_BOGUS_JS = os.path.join(BUNDLE_DIR, "x-bogus.js")
DEBUG_DIR = os.path.join(RUNTIME_DIR, "debug_dumps")


class DouyinClient:
    """Client for checking Douyin user live status."""

    def __init__(
        self, headers: Optional[Dict[str, str]] = None, debug: bool = False
    ):
        self.session = requests.Session()
        self.session.headers.update(headers or DEFAULT_HEADERS)
        self._cached_sec_uid: Optional[str] = None
        self._cached_room_id: str = ""
        self._cached_user_info: Dict[str, Any] = {}
        self._cookies_initialized = False
        self._ies_share_url: str = ""
        self.debug = debug

        if self.debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            logger.info(f"[DEBUG] 调试输出目录: {DEBUG_DIR}")

    def _dump_debug(self, name: str, content: str, is_json: bool = False) -> None:
        """Save raw response to disk for debugging."""
        if not self.debug:
            return
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
            filename = f"{timestamp}_{safe_name}"
            if is_json:
                filename += ".json"
                filepath = os.path.join(DEBUG_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    if isinstance(content, str):
                        try:
                            data = json.loads(content)
                            json.dump(data, f, ensure_ascii=False, indent=2)
                        except (json.JSONDecodeError, TypeError):
                            f.write(content)
                    else:
                        json.dump(content, f, ensure_ascii=False, indent=2)
            else:
                filename += ".html"
                filepath = os.path.join(DEBUG_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
            logger.info(f"[DEBUG] 已保存: {filepath} ({len(content)} bytes)")
        except Exception as e:
            logger.warning(f"[DEBUG] 保存失败: {e}")

    def _ensure_cookies(self) -> None:
        """Obtain initial cookies by visiting Douyin and live.douyin.com.

        The ttwid cookie from live.douyin.com is ESSENTIAL for API requests.
        Without it, the API returns empty responses even with valid a_bogus.
        """
        if self._cookies_initialized:
            return

        try:
            logger.info("获取初始 Cookie...")

            # Step 1: Visit live.douyin.com first — this gives the critical
            #         ttwid cookie that the API requires for authentication.
            resp = self.session.get(
                "https://live.douyin.com",
                timeout=15,
                allow_redirects=True,
            )
            resp.raise_for_status()
            logger.debug(
                f"live.douyin.com cookies: "
                f"{dict(self.session.cookies)}"
            )

            # Step 2: Also visit douyin.com for any additional cookies
            if "ttwid" not in self.session.cookies:
                resp2 = self.session.get(
                    "https://www.douyin.com",
                    timeout=15,
                    allow_redirects=True,
                )
                resp2.raise_for_status()
                # Extract ttwid from response if still not set
                if "ttwid" not in self.session.cookies:
                    ttwid_match = re.search(
                        r'ttwid=([^;]+)', resp2.text[:50000]
                    )
                    if ttwid_match:
                        self.session.cookies.set(
                            "ttwid",
                            ttwid_match.group(1),
                            domain=".douyin.com",
                        )

            if "ttwid" in self.session.cookies:
                logger.info("Cookie 初始化完成 (已获取 ttwid)")
            else:
                logger.warning("Cookie 初始化完成 (未获取到 ttwid)")

            self._cookies_initialized = True
        except Exception as e:
            logger.warning(f"Cookie 初始化失败: {e}，将直接尝试请求")
            self._cookies_initialized = True  # Don't keep retrying

    def resolve_short_link(self, short_url: str) -> str:
        """
        Resolve a v.douyin.com short link to the full user profile URL.

        Args:
            short_url: Short Douyin link (e.g., https://v.douyin.com/xxx/)

        Returns:
            Full profile URL or live stream URL
        """
        logger.info(f"解析短链接: {short_url}")

        try:
            resp = self.session.head(
                short_url,
                timeout=15,
                allow_redirects=True,
            )
            final_url = resp.url
            logger.info(f"解析结果: {final_url[:150]}...")

            # Extract sec_uid from the URL
            self._extract_sec_uid(final_url)

            # Detect if this is a live stream share link
            if "webcast.amemv.com" in final_url or "webcast" in final_url:
                logger.info("检测到直播分享链接!")
                # Extract room_id from /reflow/{room_id}
                room_match = re.search(r'/reflow/(\d+)', final_url)
                if room_match:
                    self._cached_room_id = room_match.group(1)
                    logger.info(f"从链接提取直播间ID: {self._cached_room_id}")
                # Also try other patterns
                if not self._cached_room_id:
                    room_match = re.search(r'live\.douyin\.com/(\d+)', final_url)
                    if room_match:
                        self._cached_room_id = room_match.group(1)

            # Save the IES share URL for later use
            if "iesdouyin.com/share/user/" in final_url:
                self._ies_share_url = final_url
                logger.info(f"保存IES分享页URL")

            return final_url
        except Exception as e:
            logger.error(f"短链接解析失败: {e}")
            raise

    def _extract_sec_uid(self, url: str) -> str:
        """Extract sec_uid from a Douyin profile URL or page content."""
        if not self._cached_sec_uid:
            # Try to extract from URL pattern: /user/{sec_uid}
            match = re.search(r'/user/([A-Za-z0-9_-]+)', url)
            if match:
                self._cached_sec_uid = match.group(1)
            else:
                # Try to extract sec_uid/sec_user_id from query params
                # (common in webcast.amemv.com and iesdouyin.com URLs)
                parsed = urlparse(url)
                qs = parse_qs(parsed.query)
                for key in ["sec_user_id", "sec_uid"]:
                    vals = qs.get(key, [])
                    if vals:
                        self._cached_sec_uid = vals[0]
                        logger.info(f"从查询参数 '{key}' 提取 sec_uid: {self._cached_sec_uid}")
                        break

        return self._cached_sec_uid or ""

    def _generate_a_bogus(self, params: str) -> Dict[str, str]:
        """
        Generate a_bogus signature using pure Python (no Node.js dependency).

        Args:
            params: URL query string to sign

        Returns:
            Dict with 'a_bogus' and 'msToken' keys
        """
        try:
            from abogus import generate_signatures
            ua = self.session.headers.get("User-Agent", "")
            a_bogus, ms_token = generate_signatures(params, ua)
            return {"a_bogus": a_bogus, "msToken": ms_token}
        except ImportError:
            logger.warning("abogus 模块未找到，签名生成失败")
            return {"a_bogus": "", "msToken": ""}
        except Exception as e:
            logger.warning(f"签名生成异常: {e}")
            return {"a_bogus": "", "msToken": ""}

    def check_live_by_html(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 1: Check live status by parsing the user profile page HTML.
        Parses embedded JSON data (RENDER_DATA, __INITIAL_STATE__, etc.)

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "html",
        }

        try:
            url = f"https://www.douyin.com/user/{sec_uid}"
            logger.info(f"[HTML检测] 访问用户主页: {url}")

            resp = self.session.get(url, timeout=15, allow_redirects=True)
            resp.raise_for_status()
            html = resp.text
            final_url = resp.url

            logger.debug(f"[HTML检测] 最终URL: {final_url}")
            logger.debug(f"[HTML检测] 页面大小: {len(html)} bytes")

            # Check if we got redirected to a captcha/login page
            if "verify" in final_url.lower() or "captcha" in final_url.lower():
                logger.warning("[HTML检测] 页面被重定向到验证页面（反爬拦截）")
                self._dump_debug("html_captcha_redirect", html)
            elif len(html) < 5000:
                logger.warning(
                    f"[HTML检测] 页面内容过短 ({len(html)} bytes)，可能是反爬页面"
                )
                self._dump_debug("html_short_response", html)

            # === Approach 1: RENDER_DATA (SSR embedded JSON) ===
            render_match = re.search(
                r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>',
                html, re.DOTALL
            )

            if render_match:
                try:
                    data_str = unquote(render_match.group(1))
                    data = json.loads(data_str)

                    # Try multiple paths through the data
                    if isinstance(data, dict):
                        # Extract room info at top level
                        for room_key in ["room", "live_room", "roomInfo"]:
                            room = data.get(room_key, {})
                            if isinstance(room, dict):
                                rid = room.get("room_id", "") or room.get("id_str", "")
                                if rid:
                                    result["room_id"] = str(rid)
                                result["title"] = room.get("title", "") or room.get("room_title", "")

                        # Navigate app data
                        app_data = data.get("app", data)
                        if isinstance(app_data, dict):
                            # Try nested paths for user info
                            for path in [
                                ["userInfo"],
                                ["user"],
                                ["profile", "user"],
                                ["userInfo", "user"],
                            ]:
                                user_info = app_data
                                for key in path:
                                    user_info = user_info.get(key, {}) if isinstance(user_info, dict) else {}
                                if isinstance(user_info, dict) and user_info.get("nickname"):
                                    result["nickname"] = user_info.get("nickname", "")
                                    live_status = user_info.get("live_status", 0)
                                    status = user_info.get("status", 0)
                                    if (live_status and int(live_status) != 0) or \
                                       (status and int(status) == 2):
                                        result["is_live"] = True
                                    break

                        # Also check top-level keys
                        for info_key in ["userInfo", "user", "authorInfo"]:
                            user_info = data.get(info_key, {})
                            if isinstance(user_info, dict) and user_info.get("nickname"):
                                if not result["nickname"]:
                                    result["nickname"] = user_info.get("nickname", "")
                                live_status = user_info.get("live_status", 0)
                                if live_status and int(live_status) != 0:
                                    result["is_live"] = True
                                break

                    self._dump_debug("html_render_data", data, is_json=True)

                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.debug(f"[HTML检测] RENDER_DATA 解析失败: {e}")

            # === Approach 2: __INITIAL_STATE__ ===
            if not result["nickname"]:
                state_match = re.search(
                    r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
                    html, re.DOTALL
                )
                if not state_match:
                    state_match = re.search(
                        r'__INITIAL_STATE__\s*=\s*({.*?});',
                        html, re.DOTALL
                    )
                if state_match:
                    try:
                        state = json.loads(state_match.group(1))
                        self._dump_debug("html_initial_state", state, is_json=True)
                        if isinstance(state, dict):
                            user_info = state.get("userInfo", {}) or state.get("user", {})
                            if isinstance(user_info, dict):
                                if not result["nickname"]:
                                    result["nickname"] = user_info.get("nickname", "")
                                live_status = user_info.get("live_status", 0)
                                if live_status and int(live_status) != 0:
                                    result["is_live"] = True
                                # Check room data
                                room = user_info.get("room", {}) or user_info.get("live_room", {})
                                if isinstance(room, dict):
                                    rid = room.get("room_id", "") or room.get("id_str", "")
                                    if rid:
                                        result["room_id"] = str(rid)
                                    result["title"] = room.get("title", "")
                    except (json.JSONDecodeError, TypeError):
                        pass

            # === Approach 3: self.__pace_f array (newer Douyin pages) ===
            if not result["nickname"]:
                pace_match = re.search(
                    r'self\.__pace_f\s*=\s*(\[.*?\]);',
                    html, re.DOTALL
                )
                if pace_match:
                    try:
                        pace_data = json.loads(pace_match.group(1))
                        for item in pace_data:
                            if isinstance(item, dict):
                                for key in ["userInfo", "user", "profile"]:
                                    ui = item.get(key, {})
                                    if isinstance(ui, dict) and ui.get("nickname"):
                                        if not result["nickname"]:
                                            result["nickname"] = ui.get("nickname", "")
                                        live_status = ui.get("live_status", 0)
                                        if live_status and int(live_status) != 0:
                                            result["is_live"] = True
                                        break
                    except (json.JSONDecodeError, TypeError):
                        pass

            # === Approach 4: Generic regex patterns in raw HTML ===
            if not result["nickname"]:
                nick_patterns = [
                    r'"nickname"\s*:\s*"([^"]+)"',
                    r'"nick_name"\s*:\s*"([^"]+)"',
                    r'"unique_id"\s*:\s*"([^"]+)"',
                ]
                for pattern in nick_patterns:
                    nick_match = re.search(pattern, html)
                    if nick_match:
                        result["nickname"] = nick_match.group(1)
                        logger.debug(f"[HTML检测] 从正则提取昵称: {result['nickname']}")
                        break

            # === Check for live status indicators in raw HTML ===
            if not result["is_live"]:
                live_patterns = [
                    r'"live_status"\s*:\s*([1-9]\d*)',
                    r'"status"\s*:\s*2\b',
                ]
                for pattern in live_patterns:
                    match = re.search(pattern, html)
                    if match:
                        logger.info(f"[HTML检测] 页面匹配直播标识: {pattern}")
                        result["is_live"] = True
                        break

                if not result["is_live"]:
                    for indicator in ['直播中', '正在直播', 'live_status":1', 'live_status": 1']:
                        if indicator in html:
                            logger.info(f"[HTML检测] 发现直播文本标识: {indicator}")
                            result["is_live"] = True
                            break

            # === Extract room_id if live ===
            if result["is_live"] and not result["room_id"]:
                room_match = re.search(r'live\.douyin\.com/(\d+)', html)
                if room_match:
                    result["room_id"] = room_match.group(1)

            # === Approach 5: Try to extract SEC_USER_ID and other identifiers ===
            if not result["nickname"]:
                sec_match = re.search(r'"sec_uid"\s*:\s*"([^"]+)"', html)
                if sec_match:
                    logger.debug(f"[HTML检测] 页面中发现 sec_uid: {sec_match.group(1)}")

            # Debug: if nothing was extracted, dump the page for analysis
            if not result["nickname"] and not result["is_live"]:
                logger.warning(
                    "[HTML检测] 未能从页面提取任何有效数据，可能页面结构已变更"
                )
                # Log a snippet of the page to help diagnose
                snippet = html[:2000]
                logger.debug(f"[HTML检测] 页面开头:\n{snippet}")
                self._dump_debug("html_parse_failed", html)

            logger.info(
                f"[HTML检测] 结果: is_live={result['is_live']}, "
                f"nickname={result['nickname']}, "
                f"room_id={result['room_id']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[HTML检测] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[HTML检测] 解析异常: {e}", exc_info=True)

        return result

    def check_live_by_iesdouyin_api(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 3: Check via iesdouyin.com API (often less strict than douyin.com).

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "iesdouyin_api",
        }

        try:
            # iesdouyin.com user share API
            url = "https://www.iesdouyin.com/web/api/v2/user/info/"
            params = {
                "sec_uid": sec_uid,
            }

            logger.info(f"[IES API] 请求: sec_uid={sec_uid}")

            resp = self.session.get(
                url,
                params=params,
                timeout=15,
                headers={
                    "Referer": f"https://www.iesdouyin.com/share/user/{sec_uid}",
                    "Accept": "application/json, text/plain, */*",
                },
            )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    self._dump_debug("ies_api_response", data, is_json=True)
                    status_code = data.get("status_code", -1)

                    if status_code == 0:
                        user_info = data.get("user_info", {}) or data.get("user", {})
                        if isinstance(user_info, dict):
                            result["nickname"] = user_info.get("nickname", "")
                            live_status = user_info.get("live_status", 0)
                            result["is_live"] = bool(live_status and int(live_status) != 0)

                            # Check room data
                            room = user_info.get("room", {})
                            if isinstance(room, dict):
                                result["room_id"] = str(room.get("rid", "") or room.get("room_id", ""))
                                result["title"] = room.get("title", "")
                                # Even if live_status=0, a room with status=2 means live
                                room_status = room.get("status", 0)
                                if room_status == 2:
                                    result["is_live"] = True
                                    logger.info(
                                        "[IES API] room.status=2 (直播中)"
                                    )

                            # Check for alternative live indicators
                            if not result["is_live"]:
                                # Some API versions use different fields
                                for alt_field in ["is_living", "living", "on_live"]:
                                    alt_val = user_info.get(alt_field)
                                    if alt_val and (alt_val is True or str(alt_val) in ("1", "true", "True")):
                                        result["is_live"] = True
                                        logger.info(
                                            f"[IES API] 通过 {alt_field} 字段检测到直播"
                                        )
                                        break

                            # DEBUG: log available keys when not live
                            if not result["is_live"]:
                                available_keys = list(user_info.keys())
                                logger.debug(
                                    f"[IES API] 用户数据字段: {available_keys}"
                                )
                                logger.debug(
                                    f"[IES API] live_status={live_status}, "
                                    f"has_room={'room' in user_info}"
                                )
                                # Log room details if present
                                if "room" in user_info:
                                    logger.debug(
                                        f"[IES API] room 数据: {user_info['room']}"
                                    )

                            logger.info(
                                f"[IES API] nickname={result['nickname']}, "
                                f"is_live={result['is_live']}, "
                                f"live_status_raw={live_status}"
                            )
                except json.JSONDecodeError:
                    logger.debug("[IES API] 响应非JSON")
                    self._dump_debug("ies_api_non_json", resp.text)
                except Exception as e:
                    logger.warning(f"[IES API] 响应处理异常: {e}")

        except requests.RequestException as e:
            logger.warning(f"[IES API] 请求失败: {e}")

        return result

    def check_live_by_api(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 2: Check live status via Douyin user profile API.
        May require a_bogus signature.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "api",
        }

        try:
            # Build API params
            params = {
                "sec_user_id": sec_uid,
                "source": "channel_pc_web",
                "publish_video_strategy_type": "2",
                "device_platform": "webapp",
                "aid": "6383",
                "channel": "channel_pc_web",
                "pc_client_type": "1",
                "version_code": "170400",
                "version_name": "17.4.0",
                "cookie_enabled": "true",
                "screen_width": "1920",
                "screen_height": "1080",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Chrome",
                "browser_version": "120.0.0.0",
                "browser_online": "true",
                "engine_name": "Blink",
                "engine_version": "120.0.0.0",
                "os_name": "Windows",
                "os_version": "10",
                "cpu_core_num": "8",
                "device_memory": "8",
                "platform": "PC",
                "downlink": "10",
                "effective_type": "4g",
                "round_trip_time": "50",
            }

            # Try adding a_bogus signature
            params_str = "&".join(f"{k}={v}" for k, v in params.items())
            bogus = self._generate_a_bogus(params_str)
            if bogus.get("a_bogus"):
                params["a_bogus"] = bogus["a_bogus"]
            if bogus.get("msToken"):
                params["msToken"] = bogus["msToken"]

            # Add msToken to cookies as well
            if bogus.get("msToken"):
                self.session.cookies.set(
                    "msToken", bogus["msToken"], domain=".douyin.com"
                )

            url = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
            logger.info(f"[API检测] 请求用户信息 API: sec_uid={sec_uid}")

            resp = self.session.get(
                url,
                params=params,
                timeout=15,
                headers={
                    "Referer": f"https://www.douyin.com/user/{sec_uid}",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    f"[API检测] 响应非JSON "
                    f"(status={resp.status_code}, "
                    f"size={len(resp.text)} bytes)"
                )
                self._dump_debug("api_non_json_response", resp.text)
                # Log first 500 chars to help diagnose
                logger.debug(
                    f"[API检测] 响应预览: {resp.text[:500]}"
                )
                # Try with a_bogus if not already tried
                if not bogus.get("a_bogus"):
                    logger.info("[API检测] 重试带 a_bogus 签名...")
                    params_str = "&".join(
                        f"{k}={v}" for k, v in params.items()
                    )
                    bogus2 = self._generate_a_bogus(params_str)
                    if bogus2.get("a_bogus"):
                        params["a_bogus"] = bogus2["a_bogus"]
                        resp2 = self.session.get(
                            url,
                            params=params,
                            timeout=15,
                            headers={
                                "Referer": f"https://www.douyin.com/user/{sec_uid}",
                                "Accept": "application/json, text/plain, */*",
                            },
                        )
                        try:
                            data = resp2.json()
                        except (json.JSONDecodeError, ValueError):
                            logger.warning(
                                "[API检测] 带签名重试仍返回非JSON"
                            )
                            self._dump_debug(
                                "api_non_json_retry", resp2.text
                            )
                            return result
                    else:
                        return result
                else:
                    return result
            self._dump_debug("api_response", data, is_json=True)

            # Navigate response structure
            status_code = data.get("status_code", -1)
            if status_code != 0:
                error_msg = data.get("status_msg", "Unknown error")
                logger.warning(f"[API检测] API返回错误: {error_msg}")
                return result

            user_data = data.get("user", {})
            if not user_data:
                logger.warning("[API检测] 响应中未找到用户数据")
                return result

            # Extract user info
            result["nickname"] = user_data.get("nickname", "")
            result["is_live"] = bool(user_data.get("live_status", 0))

            # Extract room info - check nested AND direct fields
            room_data = (
                user_data.get("room", {})
                or user_data.get("live_room", {})
                or user_data.get("room_data", {})
                or {}
            )
            if isinstance(room_data, dict):
                result["room_id"] = str(
                    room_data.get("room_id", "")
                    or room_data.get("id_str", "")
                    or room_data.get("rid", "")
                )
                result["title"] = room_data.get("title", "")

            # Also check for room_id directly on the user object (current API format)
            if not result["room_id"]:
                direct_room = (
                    user_data.get("room_id")
                    or user_data.get("room_id_str")
                )
                if direct_room:
                    result["room_id"] = str(direct_room)

            # Also check live_commerce info
            live_commerce = user_data.get("live_commerce_info", {}) or user_data.get(
                "live_agreement", {}
            )
            if isinstance(live_commerce, dict) and live_commerce.get(
                "has_live_product"
            ):
                result["has_products"] = True

            logger.info(
                f"[API检测] 用户: {result['nickname']}, "
                f"直播状态: {result['is_live']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[API检测] 请求失败: {e}")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[API检测] 响应解析失败: {e}")
        except Exception as e:
            logger.warning(f"[API检测] 未知异常: {e}")

        return result

    def check_live_by_iesdouyin_share_page(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 3: Check by scraping the iesdouyin.com share page.
        This page often has different anti-bot behavior than douyin.com.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "ies_share",
        }

        try:
            url = f"https://www.iesdouyin.com/share/user/{sec_uid}"
            logger.info(f"[IES分享页] 访问: {url}")

            resp = self.session.get(
                url,
                timeout=15,
                allow_redirects=True,
                headers={
                    "Referer": "https://www.iesdouyin.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            html = resp.text

            logger.debug(f"[IES分享页] 页面大小: {len(html)} bytes")

            # Try to find SSR embedded data
            # Pattern 1: __INITIAL_STATE__ or similar
            state_patterns = [
                r'window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>',
                r'__INITIAL_STATE__\s*=\s*({.*?});',
                r'"userInfo"\s*:\s*(\{.*?\})[,;]',
                r'"user"\s*:\s*(\{.*?\})[,;]',
            ]

            for pattern in state_patterns:
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(1))
                        if isinstance(data, dict):
                            user_info = data.get("userInfo", {}) or data.get("user", {}) or data
                            if isinstance(user_info, dict):
                                if not result["nickname"]:
                                    result["nickname"] = user_info.get("nickname", "")
                                live_status = user_info.get("live_status", 0)
                                status = user_info.get("status", 0)
                                if (live_status and int(live_status) != 0) or \
                                   (status and int(status) == 2):
                                    result["is_live"] = True

                                room = user_info.get("room", {}) or user_info.get("live_room", {})
                                if isinstance(room, dict):
                                    result["room_id"] = str(
                                        room.get("rid", "") or room.get("room_id", "")
                                    )
                                    result["title"] = room.get("title", "")
                                if result["nickname"]:
                                    break
                    except (json.JSONDecodeError, TypeError):
                        continue

            # Pattern 2: Look for embedded JSON data
            if not result["nickname"]:
                json_data_match = re.search(
                    r'<script[^>]*>\s*window\._data\s*=\s*({.*?});\s*</script>',
                    html, re.DOTALL
                )
                if not json_data_match:
                    json_data_match = re.search(
                        r'id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                        html, re.DOTALL
                    )
                if json_data_match:
                    try:
                        data = json.loads(json_data_match.group(1))
                        self._dump_debug("ies_next_data", data, is_json=True)
                        # Walk the Next.js data structure
                        if isinstance(data, dict):
                            props = data.get("props", {})
                            page_props = props.get("pageProps", {}) or props.get("page_props", {})
                            user_data = page_props.get("userInfo", {}) or page_props.get("user", {})
                            if isinstance(user_data, dict) and user_data.get("nickname"):
                                result["nickname"] = user_data.get("nickname", "")
                                if user_data.get("live_status", 0):
                                    result["is_live"] = True
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Pattern 3: Generic data in <script> tags
            if not result["nickname"]:
                script_matches = re.findall(
                    r'<script[^>]*>(.*?)</script>', html, re.DOTALL
                )
                for script in script_matches:
                    if 'nickname' in script and 'live_status' in script:
                        try:
                            nick_match = re.search(r'"nickname"\s*:\s*"([^"]+)"', script)
                            live_match = re.search(r'"live_status"\s*:\s*(\d+)', script)
                            if nick_match:
                                result["nickname"] = nick_match.group(1)
                            if live_match and int(live_match.group(1)) != 0:
                                result["is_live"] = True
                            break
                        except Exception:
                            continue

            # Pattern 4: Direct regex for live indicators
            if not result["is_live"]:
                for indicator in ['直播中', '正在直播', 'live_status":1', 'live_status": 1']:
                    if indicator in html:
                        result["is_live"] = True
                        break

            # Debug dump if nothing found
            if not result["nickname"] and not result["is_live"]:
                logger.warning("[IES分享页] 未能提取数据")
                self._dump_debug("ies_share_parse_failed", html)

            logger.info(
                f"[IES分享页] 结果: is_live={result['is_live']}, "
                f"nickname={result['nickname']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[IES分享页] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[IES分享页] 异常: {e}")

        return result

    def check_live_by_webcast_api(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 5: Check live status via Douyin webcast/live API.
        Uses the live streaming platform API which may be separate from
        the main user profile API.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "webcast_api",
        }

        try:
            # Try the webcast user/info API
            webcast_url = "https://live.douyin.com/webcast/user/info/"
            params = {
                "sec_user_id": sec_uid,
                "version_code": "170400",
                "webcast_sdk_version": "2.0.0",
                "device_platform": "web",
            }

            logger.info(f"[Webcast API] 请求: sec_uid={sec_uid}")

            resp = self.session.get(
                webcast_url,
                params=params,
                timeout=15,
                headers={
                    "Referer": f"https://live.douyin.com/",
                    "Accept": "application/json, text/plain, */*",
                },
            )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    self._dump_debug("webcast_api_response", data, is_json=True)

                    if isinstance(data, dict):
                        user_info = data.get("data", {}) or data.get("user", {})
                        if isinstance(user_info, dict):
                            result["nickname"] = user_info.get("nickname", "")
                            result["is_live"] = bool(
                                user_info.get("live_status", 0)
                                or user_info.get("status", 0) == 2
                            )

                            room = user_info.get("room", {}) or user_info.get("live_room", {})
                            if isinstance(room, dict):
                                result["room_id"] = str(
                                    room.get("room_id", "")
                                    or room.get("id_str", "")
                                )
                                result["title"] = room.get("title", "")

                except json.JSONDecodeError:
                    logger.debug("[Webcast API] 响应非JSON")
                except Exception as e:
                    logger.warning(f"[Webcast API] 解析失败: {e}")

            # Also try the room/check_alive endpoint if we have a room_id
            if not result["is_live"] and not result["room_id"]:
                # Try an alternative endpoint to find the user's room
                alt_url = "https://live.douyin.com/webcast/room/check_alive/"
                alt_params = {
                    "room_ids": "",
                    "sec_user_id": sec_uid,
                }
                try:
                    alt_resp = self.session.get(
                        alt_url,
                        params=alt_params,
                        timeout=10,
                        headers={
                            "Referer": "https://live.douyin.com/",
                            "Accept": "application/json",
                        },
                    )
                    if alt_resp.status_code == 200:
                        alt_data = alt_resp.json()
                        self._dump_debug(
                            "webcast_check_alive", alt_data, is_json=True
                        )
                        if isinstance(alt_data, dict):
                            rooms = alt_data.get("data", {}) or alt_data
                            if isinstance(rooms, dict) and rooms:
                                result["room_id"] = str(list(rooms.keys())[0])
                                result["is_live"] = True
                except Exception:
                    pass

            logger.info(
                f"[Webcast API] 结果: is_live={result['is_live']}, "
                f"nickname={result['nickname']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[Webcast API] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[Webcast API] 异常: {e}")

        return result

    def check_live_by_room_page(self, room_id: str) -> Dict[str, Any]:
        """
        Method 3: Check by accessing the live room page directly.

        Args:
            room_id: The room ID (web_rid)

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": room_id,
            "title": "",
            "method": "room_page",
        }

        if not room_id:
            return result

        try:
            url = f"https://live.douyin.com/{room_id}"
            logger.info(f"[直播间检测] 访问: {url}")

            resp = self.session.get(
                url,
                timeout=15,
                allow_redirects=True,
            )
            resp.raise_for_status()
            html = resp.text

            # Check if redirected away (means not live or room doesn't exist)
            if "live.douyin.com" not in resp.url:
                logger.info("[直播间检测] 被重定向，可能未开播")
                return result

            # Look for live indicators in the page
            live_indicators = [
                r'"status"\s*:\s*2',
                r'"live_status"\s*:\s*1',
                r'直播中',
                r'id="live-room-title"',
            ]

            for pattern in live_indicators:
                if re.search(pattern, html):
                    logger.info(f"[直播间检测] 匹配直播标识: {pattern}")
                    result["is_live"] = True
                    break

            # Extract title
            title_match = re.search(r'"title"\s*:\s*"([^"]+)"', html)
            if title_match:
                result["title"] = title_match.group(1)

        except requests.RequestException as e:
            logger.warning(f"[直播间检测] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[直播间检测] 解析异常: {e}")

        return result

    def check_live_by_webcast_info_by_user(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 6: Check live status via webcast room/info_by_user endpoint.

        This endpoint maps sec_user_id to room info and is more reliable
        than the generic webcast user/info endpoint.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "webcast_info_by_user",
        }

        try:
            url = "https://live.douyin.com/webcast/room/info_by_user/"
            params = {
                "sec_user_id": sec_uid,
                "version_code": "170400",
                "device_platform": "web",
                "webcast_sdk_version": "2.5.0",
                "cookie_enabled": "true",
                "screen_width": "1920",
                "screen_height": "1080",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Chrome",
                "browser_version": "120.0.0.0",
                "browser_online": "true",
            }

            logger.info(f"[Webcast info_by_user] 请求: sec_uid={sec_uid[:20]}...")

            resp = self.session.get(
                url,
                params=params,
                timeout=15,
                headers={
                    "Referer": f"https://live.douyin.com/",
                    "Accept": "application/json, text/plain, */*",
                },
            )

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    self._dump_debug("webcast_info_by_user", data, is_json=True)

                    if isinstance(data, dict):
                        room_data = data.get("data", {}) or data
                        if isinstance(room_data, dict):
                            # Get room info
                            room_info = room_data.get("room", {}) or room_data
                            if isinstance(room_info, dict):
                                result["room_id"] = str(
                                    room_info.get("room_id", "")
                                    or room_info.get("id_str", "")
                                )
                                result["title"] = room_info.get("title", "")
                                status = room_info.get("status", 0)
                                if status == 2:
                                    result["is_live"] = True

                            # Also check top-level data for room_id
                            if not result["room_id"]:
                                result["room_id"] = str(
                                    room_data.get("room_id", "")
                                    or room_data.get("id_str", "")
                                )
                            if not result["is_live"]:
                                status = room_data.get("status", 0)
                                if status == 2:
                                    result["is_live"] = True

                            # Try nickname from owner info
                            owner = room_data.get("owner", {}) or room_data.get("user", {})
                            if isinstance(owner, dict):
                                result["nickname"] = owner.get("nickname", "")

                except json.JSONDecodeError:
                    logger.debug("[Webcast info_by_user] 响应非JSON")
                    self._dump_debug("webcast_info_by_user_raw", resp.text)
                except Exception as e:
                    logger.warning(f"[Webcast info_by_user] 解析失败: {e}")

            elif resp.status_code == 404:
                logger.debug("[Webcast info_by_user] 用户未开播或无直播间")
            else:
                logger.debug(
                    f"[Webcast info_by_user] HTTP {resp.status_code}"
                )

            logger.info(
                f"[Webcast info_by_user] 结果: is_live={result['is_live']}, "
                f"room_id={result['room_id']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[Webcast info_by_user] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[Webcast info_by_user] 异常: {e}")

        return result

    def check_live_by_profile_live_link(self, sec_uid: str) -> Dict[str, Any]:
        """
        Method 7: Extract live room link from user's profile page HTML.

        When a user is live, their douyin.com profile page contains an
        embedded link to their live room. This method scans for that link.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with live status info
        """
        result = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "profile_live_link",
        }

        try:
            url = f"https://www.douyin.com/user/{sec_uid}"
            logger.info(f"[Profile直播链接检测] 访问: {url}")

            resp = self.session.get(
                url,
                timeout=15,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            resp.raise_for_status()
            html = resp.text

            # Look for live room URL patterns in the HTML
            # Pattern 1: live.douyin.com/{room_id} links
            live_room_matches = re.findall(
                r'live\.douyin\.com/(\d+)', html
            )
            # Pattern 2: webcast.amemv.com/reflow/{room_id}
            webcast_matches = re.findall(
                r'webcast\.amemv\.com/reflow/(\d+)', html
            )

            all_room_ids = set(
                live_room_matches + webcast_matches
            )

            if all_room_ids:
                # Use the first found room_id
                room_id = list(all_room_ids)[0]
                result["room_id"] = room_id
                logger.info(
                    f"[Profile直播链接检测] 发现直播间链接: room_id={room_id}"
                )
                # Don't assume live just from link — verify next

            # Also look for live status indicators in the profile page
            live_indicators = [
                r'"status"\s*:\s*2\b',       # room status = 2 (live)
                r'"live_status"\s*:\s*1\b',   # live_status = 1
                r'"is_living"\s*:\s*true',
                r'"living"\s*:\s*true',
                r'直播中',
                r'正在直播',
            ]

            for pattern in live_indicators:
                if re.search(pattern, html):
                    logger.info(
                        f"[Profile直播链接检测] 匹配直播标识: {pattern}"
                    )
                    result["is_live"] = True
                    break

            # Extract nickname from page
            nick_match = re.search(r'"nickname"\s*:\s*"([^"]+)"', html)
            if nick_match:
                result["nickname"] = nick_match.group(1)

            # Try to extract room_id and title from RENDER_DATA
            render_match = re.search(
                r'<script[^>]*id="RENDER_DATA"[^>]*>(.*?)</script>',
                html, re.DOTALL,
            )
            if render_match:
                try:
                    data_str = unquote(render_match.group(1))
                    data = json.loads(data_str)
                    self._dump_debug(
                        "profile_live_link_render", data, is_json=True
                    )

                    if isinstance(data, dict):
                        # Walk through possible paths for room info
                        for top_key in ["app", "root", "data"]:
                            app = data.get(top_key, {})
                            if isinstance(app, dict):
                                # Try common paths for user room info
                                for path in [
                                    ["userInfo", "room"],
                                    ["user", "room"],
                                    ["userInfo", "live_room"],
                                    ["user", "live_room"],
                                ]:
                                    node = app
                                    for key in path:
                                        node = node.get(key, {}) if isinstance(node, dict) else {}
                                    if isinstance(node, dict) and node:
                                        rid = (
                                            node.get("room_id", "")
                                            or node.get("id_str", "")
                                            or node.get("rid", "")
                                        )
                                        if rid:
                                            result["room_id"] = str(rid)
                                        if node.get("title"):
                                            result["title"] = node["title"]
                                        status = node.get("status", 0)
                                        if status == 2:
                                            result["is_live"] = True
                                        break
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            logger.info(
                f"[Profile直播链接检测] 结果: is_live={result['is_live']}, "
                f"room_id={result['room_id']}, nickname={result['nickname']}"
            )

        except requests.RequestException as e:
            logger.warning(f"[Profile直播链接检测] 请求失败: {e}")
        except Exception as e:
            logger.warning(f"[Profile直播链接检测] 异常: {e}")

        return result

    def check_live(
        self, target_url: Optional[str] = None, sec_uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Main entry point: check if a user is live.
        Tries multiple detection methods with fallback.

        Detection order:
          1. Douyin API (douyin.com/aweme/v1/web/user/profile/other/) — most reliable
          2. Webcast info_by_user (live.douyin.com/webcast/room/info_by_user/)
          3. Profile live link extraction (douyin.com user page)
          4. HTML page parsing (douyin.com user profile)
          5. IES API v2 (iesdouyin.com/web/api/v2/user/info/)
          6. Webcast API (live.douyin.com/webcast/user/info/)
          7. IES share page scraping (iesdouyin.com/share/user/)
          8. Live room page directly (if room_id known)

        Args:
            target_url: Douyin user short link or profile URL
            sec_uid: User's sec_uid (alternative to target_url)

        Returns:
            Dict with keys:
                is_live: bool
                room_id: str
                title: str
                nickname: str
                method: str (which detection method succeeded)
                timestamp: float
        """
        self._ensure_cookies()

        # Resolve sec_uid from URL if needed
        if target_url and not sec_uid:
            final_url = self.resolve_short_link(target_url)
            sec_uid = self._extract_sec_uid(final_url)

        # If the short link resolved to a live stream URL, we already have room_id
        if self._cached_room_id and not sec_uid:
            # Extract sec_uid from query params in the webcast URL
            parsed = urlparse(final_url)
            qs = parse_qs(parsed.query)
            for key in ["sec_user_id", "sec_uid"]:
                vals = qs.get(key, [])
                if vals:
                    sec_uid = vals[0]
                    self._cached_sec_uid = sec_uid
                    break

        if not sec_uid:
            logger.error("无法获取用户 sec_uid")
            return {
                "is_live": False,
                "room_id": "",
                "title": "",
                "nickname": "",
                "method": "none",
                "timestamp": time.time(),
                "error": "Could not resolve sec_uid",
            }

        logger.info(f"开始检测直播状态 (sec_uid={sec_uid[:20]}...)")

        # If the short link resolved to a webcast.amemv.com URL, this is a
        # LIVE STREAM SHARE LINK. However, Douyin may generate this URL for
        # recently ended streams as well, so we must verify with the IES API.
        if self._cached_room_id:
            logger.info(f"直播分享链接检测: room_id={self._cached_room_id}")
            # Query IES API for actual live status (not just nickname)
            nickname = ""
            ies_is_live = None  # None = API failed to determine
            try:
                ies_info = self.check_live_by_iesdouyin_api(sec_uid)
                nickname = ies_info.get("nickname", "")
                ies_is_live = ies_info.get("is_live", False)
                logger.info(
                    f"IES API返回: is_live={ies_is_live}, nickname={nickname}"
                )
            except Exception:
                pass

            # If IES API has a decisive answer, trust it.
            # A non-empty nickname means the API call succeeded (vs. network error).
            # Fall back to True only when the API couldn't be reached at all.
            if nickname and not ies_is_live:
                is_live = False
                logger.info("IES API确认未开播，覆盖webcast URL的直播假设")
            else:
                is_live = True

            result = {
                "is_live": is_live,
                "room_id": self._cached_room_id,
                "title": "",
                "nickname": nickname,
                "method": "link_direct",
                "methods_tried": ["link_direct"],
                "timestamp": time.time(),
            }
            logger.info(
                f"检测结果(LINK): is_live={is_live}, nickname={nickname}, "
                f"room_id={self._cached_room_id}"
            )
            return result

        # Collect results from all methods
        best_result: Dict[str, Any] = {
            "is_live": False,
            "room_id": "",
            "title": "",
            "nickname": "",
            "method": "combined",
            "timestamp": time.time(),
        }

        methods_tried = []
        total_methods = "8"

        # ---- Method 1: Douyin API (most reliable, now with Python a_bogus) ----
        logger.info(f"--- [1/{total_methods}] Douyin API检测 ---")
        api_result = self.check_live_by_api(sec_uid)
        methods_tried.append("api")
        if not api_result.get("room_id") and self._cached_room_id:
            api_result["room_id"] = self._cached_room_id
        self._merge_best_result(best_result, api_result)
        if api_result["is_live"]:
            logger.info("Douyin API确认直播中!")
            api_result["methods_tried"] = methods_tried
            api_result["timestamp"] = time.time()
            return api_result

        # ---- Method 2: Webcast info_by_user ----
        logger.info(f"--- [2/{total_methods}] Webcast info_by_user检测 ---")
        wc_info_result = self.check_live_by_webcast_info_by_user(sec_uid)
        methods_tried.append("webcast_info_by_user")
        self._merge_best_result(best_result, wc_info_result)
        if wc_info_result["is_live"]:
            logger.info("Webcast info_by_user确认直播中!")
            wc_info_result["methods_tried"] = methods_tried
            wc_info_result["timestamp"] = time.time()
            return wc_info_result

        # ---- Method 3: Profile live link extraction ----
        logger.info(f"--- [3/{total_methods}] Profile直播链接检测 ---")
        profile_link_result = self.check_live_by_profile_live_link(sec_uid)
        methods_tried.append("profile_live_link")
        self._merge_best_result(best_result, profile_link_result)
        if profile_link_result["is_live"]:
            logger.info("Profile直播链接确认直播中!")
            profile_link_result["methods_tried"] = methods_tried
            profile_link_result["timestamp"] = time.time()
            return profile_link_result

        # ---- Method 4: HTML page parsing ----
        logger.info(f"--- [4/{total_methods}] HTML页面检测 ---")
        html_result = self.check_live_by_html(sec_uid)
        methods_tried.append("html")
        self._merge_best_result(best_result, html_result)
        if html_result["is_live"]:
            logger.info("HTML检测确认直播中!")
            html_result["methods_tried"] = methods_tried
            html_result["timestamp"] = time.time()
            return html_result

        # ---- Method 5: IES API ----
        logger.info(f"--- [5/{total_methods}] IES API检测 ---")
        ies_api_result = self.check_live_by_iesdouyin_api(sec_uid)
        methods_tried.append("ies_api")
        self._merge_best_result(best_result, ies_api_result)
        if ies_api_result["is_live"]:
            logger.info("IES API确认直播中!")
            ies_api_result["methods_tried"] = methods_tried
            ies_api_result["timestamp"] = time.time()
            return ies_api_result

        # ---- Method 6: Webcast API (original) ----
        logger.info(f"--- [6/{total_methods}] Webcast API检测 ---")
        webcast_result = self.check_live_by_webcast_api(sec_uid)
        methods_tried.append("webcast_api")
        self._merge_best_result(best_result, webcast_result)
        if webcast_result["is_live"]:
            logger.info("Webcast API确认直播中!")
            webcast_result["methods_tried"] = methods_tried
            webcast_result["timestamp"] = time.time()
            return webcast_result

        # ---- Method 7: IES share page ----
        logger.info(f"--- [7/{total_methods}] IES分享页检测 ---")
        ies_share_result = self.check_live_by_iesdouyin_share_page(sec_uid)
        methods_tried.append("ies_share")
        self._merge_best_result(best_result, ies_share_result)
        if ies_share_result["is_live"]:
            logger.info("IES分享页确认直播中!")
            ies_share_result["methods_tried"] = methods_tried
            ies_share_result["timestamp"] = time.time()
            return ies_share_result

        # ---- Method 8: Live room page (if room_id known) ----
        room_id = (
            best_result.get("room_id")
            or wc_info_result.get("room_id")
            or profile_link_result.get("room_id")
            or html_result.get("room_id")
            or ies_api_result.get("room_id")
            or self._cached_room_id
        )
        if room_id:
            logger.info(f"--- [8/{total_methods}] 直播间页面检测 (room_id={room_id}) ---")
            room_result = self.check_live_by_room_page(room_id)
            methods_tried.append("room_page")
            self._merge_best_result(best_result, room_result)
            if room_result["is_live"]:
                logger.info("直播间页面确认直播中!")
                room_result["methods_tried"] = methods_tried
                room_result["timestamp"] = time.time()
                return room_result

        best_result["methods_tried"] = methods_tried
        best_result["timestamp"] = time.time()
        logger.info(
            f"检测完成: is_live=False, methods_tried={methods_tried}, "
            f"nickname={best_result.get('nickname', 'N/A')}"
        )
        return best_result

    @staticmethod
    def _merge_best_result(best: Dict[str, Any], new: Dict[str, Any]) -> None:
        """Merge the best fields from a new result into the accumulator."""
        if new.get("nickname") and not best.get("nickname"):
            best["nickname"] = new["nickname"]
        if new.get("room_id") and not best.get("room_id"):
            best["room_id"] = new["room_id"]
        if new.get("title") and not best.get("title"):
            best["title"] = new["title"]
        # is_live is NOT merged — the caller checks and returns early if found

    def get_user_info(self, sec_uid: str) -> Dict[str, Any]:
        """
        Get basic user info without checking live status.

        Args:
            sec_uid: User's sec_uid

        Returns:
            Dict with nickname, avatar, follower_count, etc.
        """
        self._ensure_cookies()

        try:
            url = f"https://www.douyin.com/user/{sec_uid}"
            resp = self.session.get(url, timeout=15)
            html = resp.text

            info = {"sec_uid": sec_uid}

            # Extract nickname
            nick_match = re.search(r'"nickname"\s*:\s*"([^"]+)"', html)
            if nick_match:
                info["nickname"] = nick_match.group(1)

            # Extract avatar
            avatar_match = re.search(
                r'"avatar_thumb"\s*:\s*\{[^}]*"url_list"\s*:\s*\["([^"]+)"\]',
                html,
            )
            if avatar_match:
                info["avatar"] = avatar_match.group(1)

            return info
        except Exception as e:
            logger.warning(f"获取用户信息失败: {e}")
            return {"sec_uid": sec_uid}
