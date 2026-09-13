"""Dwell 原生 Web Push。

VAPID 密钥首次使用时生成并保存在 Dwell 的 settings 表，因此 Zeabur 重启后
订阅仍然有效。浏览器订阅同样保存在数据库；主动消息只需要调用 send_push。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from . import db


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


def _send_sync(title: str, body: str, url: str) -> dict[str, int]:
    _public, private_key = ensure_vapid_keys()
    subject = os.environ.get("VAPID_SUBJECT", "mailto:dwell@localhost").strip()
    payload = json.dumps({
        "title": title[:80] or "Cloudy",
        "body": body[:240],
        "url": url or "/",
    }, ensure_ascii=False)
    subscriptions = _subscriptions()
    alive: list[dict[str, Any]] = []
    sent = 0
    failed = 0
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
            if status not in {404, 410}:
                alive.append(subscription)
            failed += 1
        except Exception:
            alive.append(subscription)
            failed += 1
    if alive != subscriptions:
        db.setting_set("push_subscriptions", json.dumps(alive, ensure_ascii=False))
    return {"sent": sent, "failed": failed, "subscriptions": len(alive)}


async def send_push(title: str, body: str, url: str = "/") -> dict[str, int]:
    """Send without blocking FastAPI's event loop."""
    if not _subscriptions():
        return {"sent": 0, "failed": 0, "subscriptions": 0}
    return await asyncio.to_thread(_send_sync, title, body, url)
