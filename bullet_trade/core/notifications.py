"""
策略消息通知工具

提供聚宽风格的 send_msg API，通过 .env 的 MESSAGE_CHANNEL 选择上报通道：
- wechat：企业微信（MESSAGE_KEY / WECHAT_MESSAGE_KEY）
- feishu：飞书（bullet_trade/config/feishu.conf）
"""

from __future__ import annotations

import configparser
import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from .globals import log
from ..utils.env_loader import get_env, get_env_bool, load_env, parse_bool

_LOGGER = logging.getLogger(__name__)
_WECHAT_WEBHOOK_TEMPLATE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
_DEFAULT_TIMEOUT = 5
_message_handler: Optional[Callable[[str], None]] = None
_ENV_LOADED = False

# 飞书机器人键名（与 feishu.conf 中 [section] 一致）
FEISHU_BOT_ALERT = "alert"
FEISHU_BOT_TRADE = "trade"
FEISHU_BOT_REPORT = "report"
_FEISHU_KNOWN_BOTS = (FEISHU_BOT_ALERT, FEISHU_BOT_TRADE, FEISHU_BOT_REPORT)
_FEISHU_CONFIG_CACHE: Optional[Dict[str, "FeishuBotConfig"]] = None

_MESSAGE_CHANNEL_ALIASES = {
    "wechat": "wechat",
    "weixin": "wechat",
    "wx": "wechat",
    "wework": "wechat",
    "qywx": "wechat",
    "wecom": "wechat",
    "feishu": "feishu",
    "lark": "feishu",
}


@dataclass(frozen=True)
class FeishuBotConfig:
    key: str
    name: str
    webhook: str
    enabled: bool
    trade_alert: bool = False


class _FeishuNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_text(self, content: str) -> bool:
        payload = {"msg_type": "text", "content": {"text": content}}
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


class _FeishuMessageQueue:
    def __init__(self) -> None:
        self._queue: queue.Queue[Tuple[str, str]] = queue.Queue()
        self._notifiers: Dict[str, _FeishuNotifier] = {}
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def configure(self, bot_key: str, webhook_url: str) -> None:
        with self._lock:
            self._notifiers[bot_key] = _FeishuNotifier(webhook_url)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker,
                    name="feishu-notify-worker",
                    daemon=True,
                )
                self._thread.start()

    def enqueue_text(self, bot_key: str, content: str) -> bool:
        if not content.strip() or bot_key not in self._notifiers:
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


_FEISHU_QUEUE = _FeishuMessageQueue()


def set_message_handler(handler: Optional[Callable[[str], None]]) -> None:
    """注册自定义消息处理函数，传入 None 可清除。"""
    global _message_handler
    _message_handler = handler


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        load_env()
    except Exception as exc:  # pragma: no cover
        _LOGGER.debug("加载 .env 失败: %s", exc)
    _ENV_LOADED = True


def _feishu_conf_paths() -> List[Path]:
    package_conf = Path(__file__).resolve().parent.parent / "config" / "feishu.conf"
    return [package_conf, Path.cwd() / "feishu.conf", Path.cwd() / "config" / "feishu.conf"]


def resolve_feishu_conf_path() -> Optional[Path]:
    _ensure_env_loaded()
    explicit = get_env("FEISHU_CONF_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for path in _feishu_conf_paths():
        if path.is_file():
            return path
    return None


def _feishu_env_webhook(bot_key: str) -> Optional[str]:
    value = get_env(f"FEISHU_{bot_key.upper()}_WEBHOOK")
    return value.strip() if value else None


def _feishu_env_enabled(bot_key: str, default: bool) -> bool:
    raw = get_env(f"FEISHU_{bot_key.upper()}_ENABLED")
    if raw is None:
        return default
    return parse_bool(raw, default=default)


def _legacy_feishu_trade_webhook() -> Optional[str]:
    for key in ("FEISHU_WEBHOOK", "FEISHU_BOT_WEBHOOK"):
        value = get_env(key)
        if value:
            return value.strip()
    return None


def _parse_feishu_conf(path: Path) -> Dict[str, FeishuBotConfig]:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    result: Dict[str, FeishuBotConfig] = {}

    for bot_key in _FEISHU_KNOWN_BOTS:
        section = bot_key if parser.has_section(bot_key) else None
        name = bot_key
        webhook = ""
        enabled = True
        trade_alert = False
        if section:
            name = parser.get(section, "name", fallback=bot_key).strip() or bot_key
            webhook = parser.get(section, "webhook", fallback="").strip()
            enabled = parse_bool(parser.get(section, "enabled", fallback="true"), default=True)
            if bot_key == FEISHU_BOT_ALERT:
                trade_alert = parse_bool(
                    parser.get(section, "trade_alert", fallback="true"),
                    default=True,
                )

        env_hook = _feishu_env_webhook(bot_key)
        if env_hook:
            webhook = env_hook
        elif bot_key == FEISHU_BOT_TRADE and not webhook:
            legacy = _legacy_feishu_trade_webhook()
            if legacy:
                webhook = legacy

        enabled = _feishu_env_enabled(bot_key, enabled)
        if bot_key == FEISHU_BOT_ALERT:
            env_trade_alert = get_env("FEISHU_ALERT_TRADE_ALERT")
            if env_trade_alert is not None:
                trade_alert = parse_bool(env_trade_alert, default=True)

        result[bot_key] = FeishuBotConfig(
            key=bot_key,
            name=name,
            webhook=webhook,
            enabled=enabled,
            trade_alert=trade_alert,
        )
    return result


def _default_feishu_configs() -> Dict[str, FeishuBotConfig]:
    result: Dict[str, FeishuBotConfig] = {}
    for bot_key in _FEISHU_KNOWN_BOTS:
        webhook = _feishu_env_webhook(bot_key) or ""
        if bot_key == FEISHU_BOT_TRADE and not webhook:
            legacy = _legacy_feishu_trade_webhook()
            if legacy:
                webhook = legacy
        result[bot_key] = FeishuBotConfig(
            key=bot_key,
            name=bot_key,
            webhook=webhook,
            enabled=_feishu_env_enabled(bot_key, default=True),
        )
    return result


def load_feishu_bot_configs(*, reload: bool = False) -> Dict[str, FeishuBotConfig]:
    global _FEISHU_CONFIG_CACHE
    if _FEISHU_CONFIG_CACHE is not None and not reload:
        return _FEISHU_CONFIG_CACHE
    _ensure_env_loaded()
    conf_path = resolve_feishu_conf_path()
    _FEISHU_CONFIG_CACHE = _parse_feishu_conf(conf_path) if conf_path else _default_feishu_configs()
    return _FEISHU_CONFIG_CACHE


def get_feishu_bot_config(bot_key: str) -> Optional[FeishuBotConfig]:
    return load_feishu_bot_configs().get(bot_key)


def get_feishu_webhook(bot_key: str = FEISHU_BOT_TRADE) -> Optional[str]:
    config = get_feishu_bot_config(bot_key)
    if not config or not config.enabled:
        return None
    webhook = (config.webhook or "").strip()
    return webhook or None


def is_feishu_bot_enabled(bot_key: str) -> bool:
    config = get_feishu_bot_config(bot_key)
    if not config or not config.enabled:
        return False
    return bool((config.webhook or "").strip())


def is_feishu_order_notify_enabled() -> bool:
    _ensure_env_loaded()
    if not is_feishu_bot_enabled(FEISHU_BOT_TRADE):
        return False
    return get_env_bool("FEISHU_ORDER_NOTIFY", default=True)


def is_feishu_trade_alert_enabled() -> bool:
    _ensure_env_loaded()
    if not is_feishu_bot_enabled(FEISHU_BOT_ALERT):
        return False
    config = get_feishu_bot_config(FEISHU_BOT_ALERT)
    return bool(config and config.trade_alert)


def _prepare_feishu_bot(bot_key: str) -> bool:
    if not is_feishu_bot_enabled(bot_key):
        return False
    webhook = get_feishu_webhook(bot_key)
    if not webhook:
        return False
    _FEISHU_QUEUE.configure(bot_key, webhook)
    return True


def enqueue_feishu_text(content: str, bot: str = FEISHU_BOT_TRADE) -> bool:
    """将文本消息放入指定飞书机器人的异步队列。"""
    if not _prepare_feishu_bot(bot):
        return False
    return _FEISHU_QUEUE.enqueue_text(bot, content)


def _has_wechat_config() -> bool:
    return bool(get_env("MESSAGE_KEY") or get_env("WECHAT_MESSAGE_KEY"))


def get_message_channel() -> Optional[str]:
    """
    读取消息上报通道。

    Returns:
        ``wechat`` | ``feishu`` | None（未配置或通道不可用）
    """
    _ensure_env_loaded()
    raw = get_env("MESSAGE_CHANNEL") or get_env("NOTIFY_CHANNEL")
    if raw:
        channel = _MESSAGE_CHANNEL_ALIASES.get(raw.strip().lower())
        if channel:
            if channel == "wechat" and not _has_wechat_config():
                _LOGGER.warning("MESSAGE_CHANNEL=wechat 但未配置 MESSAGE_KEY")
                return None
            if channel == "feishu" and not is_feishu_bot_enabled(FEISHU_BOT_TRADE):
                _LOGGER.warning("MESSAGE_CHANNEL=feishu 但飞书交易机器人未配置")
                return None
            return channel
        _LOGGER.warning("未知 MESSAGE_CHANNEL=%s，支持 wechat / feishu", raw)
        return None

    if _has_wechat_config():
        return "wechat"
    if is_feishu_bot_enabled(FEISHU_BOT_TRADE):
        return "feishu"
    return None


def is_message_notify_enabled() -> bool:
    """当前通道是否已配置且允许发送消息（含下单通知）。"""
    channel = get_message_channel()
    if channel == "wechat":
        return _has_wechat_config()
    if channel == "feishu":
        return is_feishu_order_notify_enabled()
    return False


def _send_wechat_text(text: str) -> bool:
    key = get_env("MESSAGE_KEY") or get_env("WECHAT_MESSAGE_KEY")
    if not key:
        return False
    if requests is None:
        _LOGGER.error("requests 未安装，无法发送企业微信消息")
        return False
    url = _WECHAT_WEBHOOK_TEMPLATE.format(key=key)
    payload = {"msgtype": "text", "text": {"content": text, "mentioned_list": ["@all"]}}
    try:
        response = requests.post(url, json=payload, timeout=_DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("errcode") != 0:
            _LOGGER.error("企业微信返回错误: %s", data)
            return False
        return True
    except Exception as exc:  # pragma: no cover
        _LOGGER.exception("发送企业微信消息失败: %s", exc)
        return False


def _dispatch_message(text: str, *, feishu_bot: str = FEISHU_BOT_TRADE) -> bool:
    channel = get_message_channel()
    if channel == "wechat":
        return _send_wechat_text(text)
    if channel == "feishu":
        return enqueue_feishu_text(text, bot=feishu_bot)
    return False


def send_msg(message: str, *, feishu_bot: str = FEISHU_BOT_TRADE) -> None:
    """
    发送策略/业务通知（统一入口）。

    - 始终记录 `[策略消息]` 日志。
    - 存在自定义 handler 时优先调用，异常不会向外抛出。
    - 按 ``MESSAGE_CHANNEL`` 路由到企业微信或飞书（见 ``get_message_channel()``）。
    - 飞书通道下可通过 ``feishu_bot`` 指定机器人（alert / trade / report）。
    """
    text = str(message)
    log.info(f"[策略消息] {text}")

    if _message_handler:
        try:
            _message_handler(text)
        except Exception as exc:  # pragma: no cover
            _LOGGER.exception("自定义消息处理失败: %s", exc)

    _ensure_env_loaded()
    _dispatch_message(text, feishu_bot=feishu_bot)


def format_order_notification(payload: Dict[str, Any], *, title: str = "下单通知") -> str:
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
            lines.append(f"今日涨跌：{float(day_change):+.2f}%")
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
    bot: str = FEISHU_BOT_TRADE,
    *,
    title: Optional[str] = None,
) -> bool:
    """格式化下单/交易信息并通过 send_msg 推送。"""
    if not payload:
        return False
    if not is_message_notify_enabled():
        return False
    message_title = title or ("交易告警" if bot == FEISHU_BOT_ALERT else "下单通知")
    content = format_order_notification(payload, title=message_title)
    channel = get_message_channel()
    if channel == "feishu" and bot == FEISHU_BOT_TRADE and not is_feishu_order_notify_enabled():
        return False
    if channel == "feishu" and bot == FEISHU_BOT_ALERT and not is_feishu_trade_alert_enabled():
        return False
    if channel == "feishu":
        return _dispatch_message(content, feishu_bot=bot)
    if channel == "wechat" and bot == FEISHU_BOT_TRADE:
        return _dispatch_message(content)
    return False


def _normalize_order_side(side: str) -> Optional[str]:
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
        if result != result:
            return None
        return result
    except (TypeError, ValueError):
        return None


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
        dt = current_dt or datetime.now()
        value = _fetch_pre_close(
            security,
            dt,
            bool(settings.options.get("use_real_price")),
            bool(settings.options.get("force_no_engine")),
        )
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
    side_norm = _normalize_order_side(side) or str(side or "").strip().lower()
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
    if parsed_last and pre_close and pre_close > 0:
        payload["day_change"] = round((parsed_last - pre_close) / pre_close * 100, 2)

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
    """下单成功后调用：经 send_msg 统一上报；飞书通道下买入/卖出可同步告警机器人。"""
    try:
        side_norm = _normalize_order_side(side)
        if side_norm not in ("buy", "sell"):
            _LOGGER.debug("未知买卖方向，跳过消息通知: %s", side)
            return False
        if not is_message_notify_enabled():
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

        trade_text = format_order_notification(payload, title="下单通知")
        send_msg(trade_text)
        sent = True

        channel = get_message_channel()
        if channel == "feishu" and is_feishu_trade_alert_enabled():
            alert_text = format_order_notification(payload, title="交易告警")
            if _dispatch_message(alert_text, feishu_bot=FEISHU_BOT_ALERT):
                sent = True
        return sent
    except Exception as exc:
        _LOGGER.debug("下单消息通知失败: %s", exc)
        return False


__all__ = [
    "FEISHU_BOT_ALERT",
    "FEISHU_BOT_TRADE",
    "FEISHU_BOT_REPORT",
    "FeishuBotConfig",
    "build_order_notify_payload",
    "enqueue_feishu_text",
    "format_order_notification",
    "get_feishu_bot_config",
    "get_feishu_webhook",
    "get_message_channel",
    "is_feishu_bot_enabled",
    "is_feishu_order_notify_enabled",
    "is_feishu_trade_alert_enabled",
    "is_message_notify_enabled",
    "load_feishu_bot_configs",
    "notify_order_submitted",
    "resolve_feishu_conf_path",
    "send_msg",
    "send_order_notification",
    "set_message_handler",
]
