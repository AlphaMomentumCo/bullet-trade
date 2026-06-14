feishu_robot/
├── src/
│   ├── main.py            # 主程序（读取 bullet_trade/config/feishu.conf）
│   ├── data_fetcher.py    # 数据抓取
│   ├── data_processor.py  # 数据处理
│   └── notifier.py        # 飞书通知
├── data/                  # 数据存储
│   └── history/           # 历史数据存档
├── .env                   # 环境变量（监控标的、阈值等）
├── requirements.txt
└── README.md

## 飞书机器人配置

与 BulletTrade 共用 `bullet_trade/config/feishu.conf`：

| 机器人 | 配置键 | 用途 |
|--------|--------|------|
| 告警机器人 | `[alert]` | 涨跌幅超阈值即时提醒 |
| 日报机器人 | `[report]` | 定时行情日报 |

复制示例配置：

```bash
cp bullet_trade/config/feishu.conf.example bullet_trade/config/feishu.conf
```

也可使用环境变量 `FEISHU_ALERT_WEBHOOK`、`FEISHU_REPORT_WEBHOOK` 覆盖。
