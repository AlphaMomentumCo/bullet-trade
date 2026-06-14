"""
飞书机器人通知

仿照 feishu_robot 的 webhook 用法，支持多机器人配置与异步消息队列。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .feishu_config import (
    BOT_ALERT,
    BOT_REPORT,
    BOT_TRADE,
    get_feishu_webhook,
    is_feishu_bot_enabled,
    is_order_notify_enabled,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_TIMEOUT = 5


class FeishuNotifier:
    """飞书 webhook 消息通知（与 feishu_robot/src/notifier.py 用法一致）。"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_text(self, content: str) -> bool:
        payload = {
            "msg_type": "text",
            "content": {"text": content},
        }
        return self._send(payload)

    def send_post(self, title: str, content: List[List[Dict[str, Any]]]) -> bool:
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": content,
                    }
                }
            },
        }
        return self._send(payload)

    def send_interactive_card(self, template: Dict[str, Any]) -> bool:
        payload = {
            "msg_type": "interactive",
            "card": template,
        }
        return self._send(payload)

    def _send(self, payload: Dict[str, Any]) -> bool:
        try:
            if requests is not None:
                response = requests.post(
                    self.webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=_DEFAULT_TIMEOUT,
                )
                if response.status_code != 200:
                    _LOGGER.warning("飞书 HTTP 错误: %s", response.status_code)
                    return False
                result = response.json()
            else:
                data = json.dumps(payload).encode("utf-8")
                request = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=_DEFAULT_TIMEOUT) as response:
                    body = response.read().decode("utf-8")
                result = json.loads(body) if body else {}

            if isinstance(result, dict) and (
                result.get("StatusCode") == 0 or result.get("code") == 0
            ):
                return True
            _LOGGER.warning("飞书返回错误: %s", result)
            return False
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            _LOGGER.warning("飞书消息发送失败: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover
            _LOGGER.exception("飞书消息发送异常: %s", exc)
            return False


class FeishuMessageQueue:
    """按机器人分组的异步飞书消息队列，避免阻塞下单主流程。"""

    def __init__(self) -> None:
        self._queue: queue.Queue[Tuple[str, str]] = queue.Queue()
        self._notifiers: Dict[str, FeishuNotifier] = {}
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def configure(self, bot_key: str, webhook_url: str) -> None:
        with self._lock:
            self._notifiers[bot_key] = FeishuNotifier(webhook_url)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker,
                    name="feishu-notify-worker",
                    daemon=True,
                )
                self._thread.start()

    def enqueue_text(self, bot_key: str, content: str) -> bool:
        if not content.strip():
            return False
        if bot_key not in self._notifiers:
            return False
        try:
            self._queue.put_nowait((bot_key, content))
            return True
        except queue.Full:
            _LOGGER.warning("飞书消息队列已满，丢弃消息")
            return False

    def _worker(self) -> None:
        while True:
            try:
                bot_key, content = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            notifier = self._notifiers.get(bot_key)
            if notifier is None:
                continue
            try:
                notifier.send_text(content)
            except Exception as exc:  # pragma: no cover
                _LOGGER.debug("飞书队列发送失败 [%s]: %s", bot_key, exc)
            finally:
                self._queue.task_done()


_MESSAGE_QUEUE = FeishuMessageQueue()


def _prepare_bot(bot_key: str) -> Optional[str]:
    if not is_feishu_bot_enabled(bot_key):
        return None
    webhook = get_feishu_webhook(bot_key)
    if not webhook:
        return None
    _MESSAGE_QUEUE.configure(bot_key, webhook)
    return webhook


def enqueue_feishu_text(content: str, bot: str = BOT_TRADE) -> bool:
    """将文本消息放入指定飞书机器人的通知队列。"""
    if not _prepare_bot(bot):
        return False
    return _MESSAGE_QUEUE.enqueue_text(bot, content)


def enqueue_alert_text(content: str) -> bool:
    """告警机器人消息入队。"""
    return enqueue_feishu_text(content, bot=BOT_ALERT)


def enqueue_report_text(content: str) -> bool:
    """日报机器人消息入队。"""
    return enqueue_feishu_text(content, bot=BOT_REPORT)


def format_order_notification(payload: Dict[str, Any], *, title: str = "下单通知") -> str:
    """根据可用字段组装下单/交易告警文本（缺失字段自动省略）。"""
    side = str(payload.get("side") or "").lower()
    side_label = "买入" if side in ("buy", "b") else "卖出" if side in ("sell", "s") else side or "未知"
    emoji = "📈" if side_label == "买入" else "📉" if side_label == "卖出" else "📋"

    lines = [f"{emoji} **{title}**", ""]

    name = payload.get("name")
    code = payload.get("code") or payload.get("security")
    if name and code:
        lines.append(f"**{name}** ({code})")
    elif code:
        lines.append(f"**{code}**")
    elif name:
        lines.append(f"**{name}**")

    lines.append(f"方向：{side_label}")

    amount = payload.get("amount")
    if amount is not None:
        try:
            lines.append(f"数量：{int(amount)} 股")
        except (TypeError, ValueError):
            pass

    order_value = payload.get("order_value")
    if order_value is not None:
        try:
            value = float(order_value)
            if value > 0:
                lines.append(f"金额：¥{value:,.2f}")
        except (TypeError, ValueError):
            pass

    last_price = payload.get("last_price")
    if last_price is not None:
        try:
            price = float(last_price)
            if price > 0:
                lines.append(f"现价：{price:.2f}")
        except (TypeError, ValueError):
            pass

    day_change = payload.get("day_change")
    if day_change is not None:
        try:
            change = float(day_change)
            lines.append(f"今日涨跌：{change:+.2f}%")
        except (TypeError, ValueError):
            pass

    order_price = payload.get("order_price")
    if order_price is not None:
        try:
            price = float(order_price)
            if price > 0:
                lines.append(f"委托价：{price:.2f}")
        except (TypeError, ValueError):
            pass

    order_id = payload.get("order_id")
    if order_id:
        lines.append(f"订单ID：{order_id}")

    ts = payload.get("timestamp")
    if ts:
        lines.append(f"时间：{ts}")

    return "\n".join(lines)


def send_order_notification(
    payload: Dict[str, Any],
    bot: str = BOT_TRADE,
    *,
    title: Optional[str] = None,
) -> bool:
    """格式化下单/交易信息并推送到指定飞书机器人消息队列。"""
    if not payload:
        return False
    if bot == BOT_TRADE and not is_order_notify_enabled():
        return False
    if not _prepare_bot(bot):
        return False
    message_title = title or ("交易告警" if bot == BOT_ALERT else "下单通知")
    content = format_order_notification(payload, title=message_title)
    return _MESSAGE_QUEUE.enqueue_text(bot, content)


__all__ = [
    "BOT_ALERT",
    "BOT_REPORT",
    "BOT_TRADE",
    "FeishuNotifier",
    "FeishuMessageQueue",
    "enqueue_alert_text",
    "enqueue_feishu_text",
    "enqueue_report_text",
    "format_order_notification",
    "get_feishu_webhook",
    "is_order_notify_enabled",
    "send_order_notification",
]
