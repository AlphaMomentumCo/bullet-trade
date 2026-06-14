# BulletTrade 

<p>
  <img src="docs/assets/logo.png" alt="BulletTrade Logo" width="100">
</p>

[![PyPI version](https://badge.fury.io/py/bullet-trade.svg)](https://badge.fury.io/py/bullet-trade)
[![Python version](https://img.shields.io/pypi/pyversions/bullet-trade.svg)](https://pypi.org/project/bullet-trade/)
[![License](https://img.shields.io/github/license/BulletTrade/bullet-trade.svg)](https://github.com/BulletTrade/bullet-trade/blob/main/LICENSE)

**BulletTrade** 是一个专业的量化交易系统 Python 包，提供完整的回测和实盘交易解决方案。

简体中文 | [完整文档](docs/index.md)

## ✨ 核心特性

- **🔄 聚宽兼容**：`from jqdata import *`
- **📊 多数据源**：JQData、MiniQMT、TuShare、本地缓存与远程 QMT server 均可切换。
- **⚡ 回测 & 报告**：分钟/日线回测、真实价格撮合、HTML/PDF 报告一键生成。
- **💼 实盘接入**：本地 QMT、远程 QMT server、模拟券商按需选择。
- **🧩 可扩展**：数据/券商接口基于抽象基类，便于自定义实现。
- **🔔 飞书通知**：实盘下单后可自动推送股票名称、代码、数量、金额、今日涨跌等信息到飞书机器人。

## 🚀 新手应该先看什么

如果只是运行量价策略，例如 ETF 策略，通常不需要额外购买数据，也可以在本地完成回测和运行。涉及财务数据、小市值等策略时，可以按自己的数据条件选择聚宽、TuShare 或自定义数据源，社区里也有不少用户改成 TuShare 等数据源后正常使用。

建议按下面顺序阅读：

1. [安装 Python 和运行环境](docs/python-setup.md)：先把本地 Python、虚拟环境和依赖准备好。
2. [新手入门总览](docs/beginner-guide.md)：如果需要实盘，先看这里判断应该选择方案 A 还是方案 B。
3. [方案 A：BulletTrade 本地独立运行](docs/beginner-route-a.md)：策略直接在本地 BulletTrade 里运行，并连接本地 QMT 执行交易。
4. [方案 B：聚宽模拟盘运行策略](docs/beginner-route-b.md)：策略在聚宽模拟盘运行，BulletTrade 负责接收信号并在本地 QMT 执行交易。

## 📖 文档

- [文档首页](docs/index.md):  站点 <https://bullettrade.cn/docs/>
- [环境准备：安装 Python](docs/python-setup.md)：两个方案共用的前置步骤，先装 Python，再创建虚拟环境。
- [新手入门总览](docs/beginner-guide.md)：先看 BulletTrade 目前支持的两种方案、结构图和选型方法，再进入对应文档。
- [方案 A：独立运行](docs/beginner-route-a.md)：策略在 BulletTrade 独立运行，连接本地 QMT。
- [方案 B：聚宽侧模拟盘运行](docs/beginner-route-b.md)：策略在聚宽侧模拟盘运行，BulletTrade 负责接收信号并在本地 QMT 执行。
- [快速上手](docs/quickstart.md)：三步跑通回测/实盘，聚宽策略无改直接复用。
- [配置总览](docs/config.md)：回测/本地实盘/远程实盘/聚宽接入的环境变量一览。
- [回测引擎](docs/backtest.md)：真实价格成交、分红送股处理、聚宽代码示例与 CLI 回测。
- [实盘引擎](docs/live.md)：本地 QMT 独立实盘与远程实盘流程。
- [交易支撑](docs/trade-support.md)：聚宽模拟盘接入、远程 QMT 服务与 helper 用法。
- [QMT 服务配置](docs/qmt-server.md)：bullet-trade server 的完整说明。
- [数据源指南](docs/data/DATA_PROVIDER_GUIDE.md)：聚宽、MiniQMT、Tushare 以及自定义 Provider 配置。
- [API 文档](docs/api.md)：策略可用 API、类模型与工具函数。
- [邀请贡献](docs/contributing.md): 贡献与联系方式。 

## 🔔 飞书机器人通知

飞书多机器人配置见 `bullet_trade/config/feishu.conf`（可从 `feishu.conf.example` 复制），统一经 `bullet_trade/core/notifications.py` 的 `send_msg` 上报。

在 `.env` 中用 `MESSAGE_CHANNEL` 选择 `send_msg` / 下单通知的上报通道（**二选一**）：

```env
# 企业微信
MESSAGE_CHANNEL=wechat
MESSAGE_KEY=your-wechat-bot-key

# 或飞书（webhook 写在 feishu.conf 或环境变量）
MESSAGE_CHANNEL=feishu
FEISHU_ORDER_NOTIFY=true
```

未设置 `MESSAGE_CHANNEL` 时自动推断：配置了 `MESSAGE_KEY` 则走企微，否则若飞书 `[trade]` 已配置则走飞书。

### 机器人说明

| 配置键 `[section]` | 用途 | 使用场景 |
|-------------------|------|---------|
| `alert` | 告警机器人 | 行情异动、阈值提醒；`trade_alert=true` 时买入/卖出同步告警 |
| `trade` | 交易提示机器人 | 实盘下单通知（BulletTrade 引擎） |
| `report` | 日报机器人 | 策略内 `send_msg(..., feishu_bot=FEISHU_BOT_REPORT)` |

### 配置文件示例

复制并编辑：

```bash
cp bullet_trade/config/feishu.conf.example bullet_trade/config/feishu.conf
```

`feishu.conf` 内容：

```ini
[alert]
name = 告警机器人
webhook = https://open.feishu.cn/open-apis/bot/v2/hook/alert-xxxx
enabled = true
trade_alert = true

[trade]
name = 交易提示机器人
webhook = https://open.feishu.cn/open-apis/bot/v2/hook/trade-xxxx
enabled = true

[report]
name = 日报机器人
webhook = https://open.feishu.cn/open-apis/bot/v2/hook/report-xxxx
enabled = true
```

也可通过环境变量覆盖（优先级高于 conf 文件）：

```env
FEISHU_CONF_PATH=/path/to/feishu.conf
FEISHU_ALERT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/alert-xxxx
FEISHU_TRADE_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/trade-xxxx
FEISHU_REPORT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/report-xxxx
FEISHU_ORDER_NOTIFY=true
```

兼容旧变量：`FEISHU_WEBHOOK` / `FEISHU_BOT_WEBHOOK` 会作为 `[trade]` 的 webhook 回退。

### 触发时机（交易提示机器人）

以下路径在**下单成功提交到券商后**会自动入队发送，不阻塞主流程：

| 运行方式 | 触发位置 |
|---------|---------|
| 本地 QMT 实盘（方案 A） | `bullet_trade/core/live_engine.py` |
| 远程 QMT server / 聚宽 helper（方案 B） | `bullet_trade/server/adapters/qmt.py` |

### 消息字段

消息仅包含**当前能获取到的字段**，缺失数据会自动省略：

| 字段 | 说明 |
|------|------|
| 股票名称 | 来自 `get_security_info` |
| 股票代码 | 策略标的代码，如 `600519.XSHG` |
| 方向 | 买入 / 卖出 |
| 下单数量 | 股数 |
| 下单金额 | 委托价（或现价）× 数量 |
| 现价 | 下单时最新价 |
| 今日涨跌 | 现价相对昨收的涨跌幅（%） |
| 委托价 | 限价或市价保护价 |
| 订单 ID | 券商返回的订单号 |

### 代码接口

统一入口：`bullet_trade/core/notifications.py`

```python
from bullet_trade.core.notifications import send_msg, notify_order_submitted

# 策略消息（按 MESSAGE_CHANNEL 路由到企微或飞书）
send_msg("策略启动")

# 下单通知（同样走 send_msg 路由）
notify_order_submitted(
    security="600519.XSHG",
    side="buy",
    amount=100,
    order_price=1850.0,
    last_price=1848.5,
    order_id="123456",
)
```

高级用法（飞书多机器人、手动格式化等）：

```python
from bullet_trade.core.notifications import (
    FEISHU_BOT_ALERT,
    FEISHU_BOT_TRADE,
    get_message_channel,
    send_order_notification,
    format_order_notification,
)

# 查看当前通道
get_message_channel()  # "wechat" | "feishu" | None

# 飞书通道下指定机器人（仅 MESSAGE_CHANNEL=feishu 时生效）
send_msg("自定义告警", feishu_bot=FEISHU_BOT_ALERT)
```

### 消息示例

```text
📈 **下单通知**

**贵州茅台** (600519.XSHG)
方向：买入
数量：100 股
金额：¥185,000.00
现价：1850.00
今日涨跌：+1.25%
订单ID：123456
时间：2026-06-14 10:15:30
```

## 🔗 链接

- GitHub 仓库：https://github.com/BulletTrade/bullet-trade
- 官方站点：https://bullettrade.cn/


## 📄 许可证

[MIT License](LICENSE)

## 联系与支持

如需交流或反馈，低佣开通QMT等，可扫码添加微信，并在 Issue/PR 中提出建议：

<img src="docs/assets/wechat-contact.png" alt="微信二维码" width="180">

---

**⚠️ 风险提示：** 量化交易存在高风险，因策略、配置或软件缺陷/网络异常等导致的任何损失由使用者自行承担，请先在仿真/小仓位充分验证。
