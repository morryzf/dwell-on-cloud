"""Dwell 原生 Web Push。

VAPID 密钥首次使用时生成并保存在 Dwell 的 settings 表，因此 Zeabur 重启后
订阅仍然有效。浏览器订阅同样保存在数据库；主动消息只需要调用 send_push。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from . import db

DEFAULT_VAPID_SUBJECT = "https://github.com/morryzf/dwell-on-cloud"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def ensure_vapid_keys() -> tuple[str, str]:
    """Return ``(public_b64url, private_der_b64url)`` and create a stable pair."""
    public_key = db.setting_get("vapid_public_key", "").strip()
    private_key = db.setting_get("vapid_private_key", "").strip()
    if public_key and private_key:
        return public_key, private_key

    private = ec.generate_private_key(ec.SECP256R1())
    private_key = _b64url(private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    public_key = _b64url(public_raw)
    db.setting_set("vapid_private_key", private_key)
    db.setting_set("vapid_public_key", public_key)
    return public_key, private_key


def public_key() -> str:
    return ensure_vapid_keys()[0]


def _vapid_subject() -> str:
    """Return a public contact URI accepted by Apple Push."""
    configured = os.environ.get("VAPID_SUBJECT", "").strip()
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme == "https" and parsed.hostname not in {None, "localhost"}:
            return configured
        if parsed.scheme == "mailto" and "@" in parsed.path:
            domain = parsed.path.rsplit("@", 1)[-1].lower()
            if domain and domain != "localhost" and "." in domain:
                return configured
    return DEFAULT_VAPID_SUBJECT


def _subscriptions() -> list[dict[str, Any]]:
    raw = db.setting_get("push_subscriptions", "")
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict) and item.get("endpoint")]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # 兼容此前只保存一个设备的版本。
    legacy = db.setting_get("push_subscription", "")
    if legacy:
        try:
            value = json.loads(legacy)
            if isinstance(value, dict) and value.get("endpoint"):
                return [value]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return []


def save_subscription(subscription: dict[str, Any]) -> int:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("推送订阅内容不完整")

    clean = {
        "endpoint": endpoint,
        "expirationTime": subscription.get("expirationTime"),
        "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
    }
    items = [item for item in _subscriptions() if item.get("endpoint") != endpoint]
    items.append(clean)
    items = items[-8:]
    db.setting_set("push_subscriptions", json.dumps(items, ensure_ascii=False))
    # 新版写入后清空旧单设备值，避免同一个 endpoint 被发送两次。
    db.setting_set("push_subscription", "")
    return len(items)


def subscription_count() -> int:
    return len(_subscriptions())


def remove_subscription(endpoint: str) -> int:
    """Forget one browser without disturbing notifications on other devices."""
    endpoint = endpoint.strip()
    items = [item for item in _subscriptions() if item.get("endpoint") != endpoint]
    db.setting_set("push_subscriptions", json.dumps(items, ensure_ascii=False))
    return len(items)


def _push_host(subscription: dict[str, Any]) -> str:
    try:
        return (urlsplit(str(subscription.get("endpoint") or "")).hostname or "未知主机")[:160]
    except ValueError:
        return "无效主机"


def _safe_push_failure(subscription: dict[str, Any], exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    response_text = str(getattr(response, "text", "") or str(exc) or "").strip()
    response_text = re.sub(r"https?://\S+", "[地址已隐藏]", response_text)
    response_text = re.sub(r"[A-Za-z0-9_-]{24,}", "[令牌已隐藏]", response_text)
    response_text = re.sub(r"\s+", " ", response_text)[:180]
    return {
        "host": _push_host(subscription),
        "status": int(status) if isinstance(status, int) else None,
        "kind": type(exc).__name__,
        "reason": response_text,
    }


def _failure_summary(failures: list[dict[str, Any]]) -> str:
    parts = []
    for failure in failures[:3]:
        item = f"主机 {failure['host']}"
        if failure.get("status"):
            item += f" · HTTP {failure['status']}"
        item += f" · {failure['kind']}"
        if failure.get("reason"):
            item += f" · {failure['reason']}"
        parts.append(item)
    return "；".join(parts)


def _send_sync(title: str, body: str, url: str) -> dict[str, Any]:
    _public, private_key = ensure_vapid_keys()
    subject = _vapid_subject()
    payload = json.dumps({
        "title": title[:80] or "Cloudy",
        "body": body[:240],
        "url": url or "/",
    }, ensure_ascii=False)
    subscriptions = _subscriptions()
    alive: list[dict[str, Any]] = []
    sent = 0
    failed = 0
    failures: list[dict[str, Any]] = []
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
                ttl=60 * 60,
            )
            alive.append(subscription)
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            failures.append(_safe_push_failure(subscription, exc))
            if status not in {404, 410}:
                alive.append(subscription)
            failed += 1
        except Exception as exc:
            failures.append(_safe_push_failure(subscription, exc))
            alive.append(subscription)
            failed += 1
    if alive != subscriptions:
        db.setting_set("push_subscriptions", json.dumps(alive, ensure_ascii=False))
    return {
        "sent": sent, "failed": failed, "subscriptions": len(alive),
        "diagnostic": _failure_summary(failures),
        "status_code": next(
            (item["status"] for item in failures if item.get("status")), None
        ),
    }


async def send_push(title: str, body: str, url: str = "/") -> dict[str, Any]:
    """Send without blocking FastAPI's event loop."""
    started = time.perf_counter()
    try:
        log_id = db.system_log_start("push_delivery", "push_delivery")
    except Exception:
        log_id = ""
    if not _subscriptions():
        result = {
            "sent": 0, "failed": 0, "subscriptions": 0,
            "diagnostic": "服务端没有已保存的手机订阅", "status_code": None,
        }
    else:
        result = await asyncio.to_thread(_send_sync, title, body, url)
    if log_id:
        try:
            db.system_log_finish(
                log_id, "success" if result["sent"] else "error",
                round((time.perf_counter() - started) * 1000),
                status_code=result.get("status_code"),
                detail=result.get("diagnostic") or (
                    f"成功发送 {result['sent']} 条；失败 {result['failed']} 条"
                ),
            )
        except Exception:
            pass
    return result
