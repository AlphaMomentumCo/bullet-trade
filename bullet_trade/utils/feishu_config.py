"""
飞书机器人配置

从 feishu.conf 读取多机器人 webhook，并支持环境变量覆盖。
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .env_loader import get_env, get_env_bool, load_env, parse_bool

_LOGGER = logging.getLogger(__name__)

# 机器人键名（与 feishu.conf 中 [section] 一致）
BOT_ALERT = "alert"
BOT_TRADE = "trade"
BOT_REPORT = "report"

KNOWN_BOTS = (BOT_ALERT, BOT_TRADE, BOT_REPORT)

_ENV_LOADED = False
_CONFIG_CACHE: Optional[Dict[str, "FeishuBotConfig"]] = None


@dataclass(frozen=True)
class FeishuBotConfig:
    key: str
    name: str
    webhook: str
    enabled: bool
    trade_alert: bool = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        load_env()
    except Exception as exc:  # pragma: no cover
        _LOGGER.debug("加载 .env 失败: %s", exc)
    _ENV_LOADED = True


def _default_conf_paths() -> list[Path]:
    paths: list[Path] = []
    package_conf = Path(__file__).resolve().parent.parent / "config" / "feishu.conf"
    paths.append(package_conf)
    paths.append(Path.cwd() / "feishu.conf")
    paths.append(Path.cwd() / "config" / "feishu.conf")
    return paths


def resolve_feishu_conf_path() -> Optional[Path]:
    _ensure_env_loaded()
    explicit = get_env("FEISHU_CONF_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return path
        _LOGGER.warning("FEISHU_CONF_PATH 指向的文件不存在: %s", path)
        return None

    for path in _default_conf_paths():
        if path.is_file():
            return path
    return None


def _env_webhook(bot_key: str) -> Optional[str]:
    env_key = f"FEISHU_{bot_key.upper()}_WEBHOOK"
    value = get_env(env_key)
    if value:
        return value.strip()
    return None


def _env_enabled(bot_key: str, default: bool) -> bool:
    env_key = f"FEISHU_{bot_key.upper()}_ENABLED"
    raw = get_env(env_key)
    if raw is None:
        return default
    return parse_bool(raw, default=default)


def _legacy_trade_webhook() -> Optional[str]:
    for key in ("FEISHU_WEBHOOK", "FEISHU_BOT_WEBHOOK"):
        value = get_env(key)
        if value:
            return value.strip()
    return None


def _parse_conf_file(path: Path) -> Dict[str, FeishuBotConfig]:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    result: Dict[str, FeishuBotConfig] = {}

    sections = set(parser.sections()) | set(KNOWN_BOTS)
    for bot_key in sections:
        if bot_key not in KNOWN_BOTS:
            continue
        section = bot_key if parser.has_section(bot_key) else None
        name = bot_key
        webhook = ""
        enabled = True
        trade_alert = False
        if section:
            name = parser.get(section, "name", fallback=bot_key).strip() or bot_key
            webhook = parser.get(section, "webhook", fallback="").strip()
            enabled = parse_bool(parser.get(section, "enabled", fallback="true"), default=True)
            if bot_key == BOT_ALERT:
                trade_alert = parse_bool(
                    parser.get(section, "trade_alert", fallback="true"),
                    default=True,
                )

        env_hook = _env_webhook(bot_key)
        if env_hook:
            webhook = env_hook
        elif bot_key == BOT_TRADE and not webhook:
            legacy = _legacy_trade_webhook()
            if legacy:
                webhook = legacy

        enabled = _env_enabled(bot_key, enabled)
        if bot_key == BOT_ALERT:
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


def _default_bot_configs() -> Dict[str, FeishuBotConfig]:
    result: Dict[str, FeishuBotConfig] = {}
    for bot_key in KNOWN_BOTS:
        webhook = _env_webhook(bot_key) or ""
        if bot_key == BOT_TRADE and not webhook:
            legacy = _legacy_trade_webhook()
            if legacy:
                webhook = legacy
        result[bot_key] = FeishuBotConfig(
            key=bot_key,
            name=bot_key,
            webhook=webhook,
            enabled=_env_enabled(bot_key, default=True),
        )
    return result


def load_feishu_bot_configs(*, reload: bool = False) -> Dict[str, FeishuBotConfig]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE

    _ensure_env_loaded()
    conf_path = resolve_feishu_conf_path()
    if conf_path:
        _CONFIG_CACHE = _parse_conf_file(conf_path)
    else:
        _CONFIG_CACHE = _default_bot_configs()
    return _CONFIG_CACHE


def get_feishu_bot_config(bot_key: str) -> Optional[FeishuBotConfig]:
    configs = load_feishu_bot_configs()
    return configs.get(bot_key)


def get_feishu_webhook(bot_key: str = BOT_TRADE) -> Optional[str]:
    config = get_feishu_bot_config(bot_key)
    if not config or not config.enabled:
        return None
    webhook = (config.webhook or "").strip()
    return webhook or None


def is_feishu_bot_enabled(bot_key: str) -> bool:
    config = get_feishu_bot_config(bot_key)
    if not config:
        return False
    if not config.enabled:
        return False
    return bool((config.webhook or "").strip())


def is_order_notify_enabled() -> bool:
    _ensure_env_loaded()
    if not is_feishu_bot_enabled(BOT_TRADE):
        return False
    return get_env_bool("FEISHU_ORDER_NOTIFY", default=True)


def is_trade_alert_enabled() -> bool:
    """告警机器人是否接收买入/卖出交易告警。"""
    _ensure_env_loaded()
    if not is_feishu_bot_enabled(BOT_ALERT):
        return False
    config = get_feishu_bot_config(BOT_ALERT)
    if not config:
        return False
    return bool(config.trade_alert)


__all__ = [
    "BOT_ALERT",
    "BOT_TRADE",
    "BOT_REPORT",
    "KNOWN_BOTS",
    "FeishuBotConfig",
    "get_feishu_bot_config",
    "get_feishu_webhook",
    "is_feishu_bot_enabled",
    "is_order_notify_enabled",
    "is_trade_alert_enabled",
    "load_feishu_bot_configs",
    "resolve_feishu_conf_path",
]
