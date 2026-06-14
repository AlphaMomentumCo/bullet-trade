"""
飞书三机器人 demo（兼容 jqdata send_msg / set_message_handler 框架）

用法一 — 与策略相同写法（推荐）：

    .env 配置：
        MESSAGE_CHANNEL=feishu
        FEISHU_BROADCAST_ALL=true

    然后运行本文件或 pytest。

用法二 — pytest：
    pytest tests/test_demo.py -m requires_network -s
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 若 .env 未指定，demo 默认走飞书三机器人广播
os.environ.setdefault("MESSAGE_CHANNEL", "feishu")
os.environ.setdefault("FEISHU_BROADCAST_ALL", "true")

from jqdata import send_msg, set_message_handler  # noqa: E402

_HANDLER_CALLS: list[str] = []


def my_handler(text: str) -> None:
    _HANDLER_CALLS.append(text)
    print(f"[handler] {text}")


def _demo_message() -> str:
    return (
        f"BulletTrade 飞书三机器人 demo\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def run_demo() -> None:
    """执行 demo：handler 回调一次，消息广播到三个飞书机器人。"""
    from bullet_trade.core import notifications

    _HANDLER_CALLS.clear()
    set_message_handler(my_handler)
    message = _demo_message()
    send_msg(message)

    if notifications.get_message_channel() == "feishu":
        notifications._FEISHU_QUEUE.flush()

    assert _HANDLER_CALLS == [message], "set_message_handler 应收到与 send_msg 相同的内容"
    print(f"[demo] handler 已触发，消息已交给 send_msg 框架：{message!r}")


@pytest.mark.requires_network
def test_demo_send_msg_with_handler_to_three_feishu_bots():
    from bullet_trade.core import notifications

    if not any(
        notifications.is_feishu_bot_enabled(bot)
        for bot in (
            notifications.FEISHU_BOT_ALERT,
            notifications.FEISHU_BOT_TRADE,
            notifications.FEISHU_BOT_REPORT,
        )
    ):
        pytest.skip("未配置飞书 webhook，跳过 demo")

    run_demo()


if __name__ == "__main__":
    try:
        run_demo()
    except AssertionError as exc:
        print(f"demo 失败: {exc}")
        raise SystemExit(1) from exc
