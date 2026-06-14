"""
下单飞书通知

在实盘下单成功后收集可用字段并推送到飞书机器人消息队列。
买入、卖出均会触发交易提示机器人；若启用 trade_alert，同步推送告警机器人。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .feishu_config import BOT_ALERT, BOT_TRADE, is_trade_alert_enabled
from .feishu_notifier import send_order_notification

_LOGGER = logging.getLogger(__name__)


def _normalize_side(side: str) -> Optional[str]:
    value = str(side or "").strip().lower()
    if value in ("buy", "b", "买入"):
        return "buy"
    if value in ("sell", "s", "卖出"):
        return "sell"
    return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        result = float(value)
        if result != result:  # NaN
            return None
        return result
    except (TypeError, ValueError):
        return None


def _compute_day_change(last_price: Optional[float], pre_close: Optional[float]) -> Optional[float]:
    if last_price is None or pre_close is None:
        return None
    if last_price <= 0 or pre_close <= 0:
        return None
    return round((last_price - pre_close) / pre_close * 100, 2)


def _lookup_security_name(security: str) -> Optional[str]:
    try:
        from bullet_trade.data.api import get_security_info

        info = get_security_info(security) or {}
        name = info.get("display_name") or info.get("name")
        if name:
            return str(name).strip() or None
    except Exception as exc:
        _LOGGER.debug("获取证券名称失败 %s: %s", security, exc)
    return None


def _lookup_pre_close(security: str, current_dt: Optional[datetime] = None) -> Optional[float]:
    pre_close_keys = ("pre_close", "preClose", "preclose", "last_close", "lastClose")

    try:
        from bullet_trade.data import api as data_api

        provider = getattr(data_api, "_provider", None)
        get_live = getattr(provider, "get_live_current", None)
        if callable(get_live):
            snap = get_live(security)
            if isinstance(snap, dict):
                for key in pre_close_keys:
                    value = _safe_float(snap.get(key))
                    if value and value > 0:
                        return value
    except Exception as exc:
        _LOGGER.debug("从 live 快照获取昨收失败 %s: %s", security, exc)

    try:
        from bullet_trade.data.api import _fetch_pre_close
        from bullet_trade.core.settings import get_settings

        settings = get_settings()
        use_real_price = bool(settings.options.get("use_real_price"))
        force_no_engine = bool(settings.options.get("force_no_engine"))
        dt = current_dt or datetime.now()
        value = _fetch_pre_close(security, dt, use_real_price, force_no_engine)
        parsed = _safe_float(value)
        if parsed and parsed > 0:
            return parsed
    except Exception as exc:
        _LOGGER.debug("从历史行情获取昨收失败 %s: %s", security, exc)

    return None


def build_order_notify_payload(
    *,
    security: str,
    side: str,
    amount: int,
    order_price: Optional[float] = None,
    last_price: Optional[float] = None,
    order_id: Optional[str] = None,
    current_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """收集下单通知字段；无法获取的数据不会写入 payload。"""
    side_norm = _normalize_side(side) or str(side or "").strip().lower()
    payload: Dict[str, Any] = {
        "code": security,
        "security": security,
        "side": side_norm,
        "amount": int(amount),
        "timestamp": (current_dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
    }

    name = _lookup_security_name(security)
    if name:
        payload["name"] = name

    parsed_last = _safe_float(last_price)
    if parsed_last and parsed_last > 0:
        payload["last_price"] = parsed_last

    parsed_order_price = _safe_float(order_price)
    if parsed_order_price and parsed_order_price > 0:
        payload["order_price"] = parsed_order_price

    price_basis = parsed_order_price if parsed_order_price and parsed_order_price > 0 else parsed_last
    if price_basis and price_basis > 0 and amount > 0:
        payload["order_value"] = round(price_basis * int(amount), 2)

    pre_close = _lookup_pre_close(security, current_dt=current_dt)
    day_change = _compute_day_change(parsed_last, pre_close)
    if day_change is not None:
        payload["day_change"] = day_change

    if order_id:
        payload["order_id"] = str(order_id)

    return payload


def notify_order_submitted(
    *,
    security: str,
    side: str,
    amount: int,
    order_price: Optional[float] = None,
    last_price: Optional[float] = None,
    order_id: Optional[str] = None,
    current_dt: Optional[datetime] = None,
) -> bool:
    """下单成功后调用：买入/卖出均推送交易提示；可选同步推送告警机器人。"""
    try:
        side_norm = _normalize_side(side)
        if side_norm not in ("buy", "sell"):
            _LOGGER.debug("未知买卖方向，跳过飞书通知: %s", side)
            return False

        payload = build_order_notify_payload(
            security=security,
            side=side_norm,
            amount=amount,
            order_price=order_price,
            last_price=last_price,
            order_id=order_id,
            current_dt=current_dt,
        )

        sent = False
        if send_order_notification(payload, bot=BOT_TRADE):
            sent = True
        if is_trade_alert_enabled() and send_order_notification(payload, bot=BOT_ALERT):
            sent = True
        return sent
    except Exception as exc:
        _LOGGER.debug("飞书下单通知失败: %s", exc)
        return False


__all__ = ["build_order_notify_payload", "notify_order_submitted"]
