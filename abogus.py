"""
Pure-Python Douyin a_bogus / msToken Signature Generator.

Translates the x-bogus.js algorithm so we don't depend on Node.js
at runtime. Based on the open-source DouyinLiveRecorder project.

Reference: https://github.com/ihmily/DouyinLiveRecorder
"""

import hashlib
import random
import string
import time
from typing import Tuple

# Douyin custom Base64 alphabet
_BASE64_ALPHABET = (
    "Dkdpgh4ZKsQB80/Mfvw36XI1R25-"
    "WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
)


def _rc4(key: bytes, data: bytes) -> bytes:
    """Standard RC4 stream cipher."""
    key_len = len(key)
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % key_len]) & 0xFF
        S[i], S[j] = S[j], S[i]

    i = j = 0
    result = bytearray(len(data))
    for k in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        result[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(result)


def _md5_bytes(data: bytes) -> bytes:
    """Return MD5 hash as raw bytes (16 bytes)."""
    return hashlib.md5(data).digest()


def _custom_base64_encode(data: bytes) -> str:
    """Encode bytes using Douyin's custom base64 alphabet."""
    result = []
    length = len(data)

    for i in range(0, length, 3):
        b1 = data[i]
        b2 = data[i + 1] if i + 1 < length else 0
        b3 = data[i + 2] if i + 2 < length else 0

        combined = (b1 << 16) | (b2 << 8) | b3

        result.append(_BASE64_ALPHABET[(combined >> 18) & 0x3F])
        result.append(_BASE64_ALPHABET[(combined >> 12) & 0x3F])

        if i + 1 < length:
            result.append(_BASE64_ALPHABET[(combined >> 6) & 0x3F])
        if i + 2 < length:
            result.append(_BASE64_ALPHABET[combined & 0x3F])

    return "".join(result)


def _generate_19_array(
    md5_params1: bytes,
    md5_params2: bytes,
    md5_ua: bytes,
    timestamp: int,
) -> list:
    """Generate the 19-element array used in a_bogus."""
    cvs = 536919696  # Fixed canvas fingerprint

    return [
        64,                          # [0]  fixed
        1,                           # [1]  fixed
        md5_params1[14],             # [2]
        md5_params2[14],             # [3]
        69,                          # [4]  'E'
        98,                          # [5]  'b'
        (timestamp >> 8) & 255,      # [6]
        (cvs >> 24) & 255,           # [7]
        77,                          # [8]  'M'
        0.00390625,                  # [9]  fixed float
        8,                           # [10] fixed
        124,                         # [11] '|'
        md5_ua[14],                  # [12]
        md5_params1[15],             # [13]
        md5_params2[15],             # [14]
        (timestamp >> 16) & 255,     # [15]
        timestamp & 255,             # [16]
        (cvs >> 16) & 255,           # [17]
        cvs & 255,                   # [18]
    ]


def _array_to_bytes(arr: list) -> bytes:
    """Convert 19-element array to byte sequence."""
    result = bytearray()
    for val in arr:
        if isinstance(val, int):
            result.append(val & 0xFF)
        else:
            # Float value – multiply by 256, take integer part
            result.append(int(val * 256) & 0xFF)
    return bytes(result)


def generate_abogus(params: str, user_agent: str) -> str:
    """
    Generate a_bogus signature for Douyin API requests.

    Args:
        params: URL query string (e.g. "aid=6383&sec_user_id=...")
        user_agent: Browser User-Agent string

    Returns:
        The a_bogus signature string
    """
    timestamp = int(time.time())

    # Double MD5 of params
    md5_1 = _md5_bytes(params.encode("utf-8"))
    md5_2 = _md5_bytes(md5_1)

    # Double MD5 of empty body
    body_md5_1 = _md5_bytes(b"")
    body_md5_2 = _md5_bytes(body_md5_1)

    # RC4 encrypt UA (key = [0, 1, 14]), custom-base64, then MD5
    ua_key = bytes([0, 1, 14])
    ua_encrypted = _rc4(ua_key, user_agent.encode("utf-8"))
    ua_b64 = _custom_base64_encode(ua_encrypted)
    ua_md5 = _md5_bytes(ua_b64.encode("utf-8"))

    # Generate 19-element array → bytes → RC4 encrypt with key [0xFF]
    arr19 = _generate_19_array(md5_1, md5_2, ua_md5, timestamp)
    arr_bytes = _array_to_bytes(arr19)
    rc4_key = bytes([0xFF])
    encrypted = _rc4(rc4_key, arr_bytes)

    # Prepend STX + ÿ (0x02, 0xFF)
    final_bytes = bytes([0x02, 0xFF]) + encrypted

    return _custom_base64_encode(final_bytes)


def generate_ms_token() -> str:
    """Generate a random msToken (128 lowercase alphanumeric chars)."""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(128))


def generate_signatures(params: str, user_agent: str) -> Tuple[str, str]:
    """
    Generate both a_bogus and msToken.

    Args:
        params: URL query string
        user_agent: Browser User-Agent string

    Returns:
        Tuple of (a_bogus, msToken)
    """
    return generate_abogus(params, user_agent), generate_ms_token()


# ===== CLI for testing =====
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print(
            'Usage: python abogus.py "<url_params>" "<user_agent>"',
            file=sys.stderr,
        )
        sys.exit(1)

    params = sys.argv[1]
    ua = sys.argv[2] if len(sys.argv) > 2 else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    a_bogus, ms_token = generate_signatures(params, ua)
    import json

    print(json.dumps({"a_bogus": a_bogus, "msToken": ms_token}))
