"""Safe error categories shared by detection, scheduling and notifications."""

import socket

import requests


class ProfileDNSError(requests.ConnectionError):
    """The profile redirect host could not be resolved."""


class ProfileLinkError(ValueError):
    """A safe, user-facing explanation for a rejected profile link."""


def transient_network_error(exc: Exception) -> bool:
    return isinstance(exc, (requests.ConnectionError, requests.Timeout, socket.gaierror))


def failure_reason(exc: Exception) -> str:
    """Never expose request URLs, identifiers or credentials in alerts."""
    if isinstance(exc, ProfileLinkError):
        return str(exc)
    if isinstance(exc, (ProfileDNSError, socket.gaierror)):
        return "DNS 域名解析失败"
    if isinstance(exc, requests.Timeout):
        return "网络请求超时"
    if isinstance(exc, requests.ConnectionError):
        return "网络连接失败"
    if isinstance(exc, requests.RequestException):
        return "服务请求失败"
    if isinstance(exc, ValueError):
        return "主播链接无效、未通过安全校验或无法提取账号"
    return "监控任务异常"
