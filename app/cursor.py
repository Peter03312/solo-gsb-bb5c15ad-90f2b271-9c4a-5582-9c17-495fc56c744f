"""差异核对分页游标：base64url(JSON) + HMAC-SHA256 签名。

游标不透明、不可伪造；签名密钥持久化在 app_meta，重启后沿用，因此跨重启
游标仍有效。游标只承载快照定位与稳定分页位置，核算数据全部来自首次请求
冻结的 diff_snapshots 行。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict


class CursorError(ValueError):
    """游标缺失/损坏/签名无效（HTTP 400 invalid_cursor）。"""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except Exception as exc:  # noqa: BLE001
        raise CursorError("游标编码损坏，无法解析") from exc


def encode_cursor(payload: Dict[str, Any], secret: str) -> str:
    body = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def decode_cursor(token: str, secret: str) -> Dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        raise CursorError("游标格式无效")
    body, _, sig = token.rpartition(".")
    expected = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise CursorError("游标签名无效（可能已被篡改或密钥已变更）")
    try:
        payload = json.loads(_b64decode(body).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CursorError("游标内容损坏，无法解析") from exc
    required = {"snapshot_id", "old_revision", "new_revision", "since", "until", "page_size", "at"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise CursorError("游标内容不完整")
    return payload
