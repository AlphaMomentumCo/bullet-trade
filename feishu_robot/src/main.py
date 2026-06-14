import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from data_fetcher import FinanceDataFetcher
from data_processor import DataProcessor
from notifier import FeishuNotifier
from datetime import datetime
import schedule
import time

# 加载环境变量
load_dotenv()

# 接入 BulletTrade 飞书多机器人配置
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bullet_trade.utils.feishu_config import (  # noqa: E402
    BOT_ALERT,
    BOT_REPORT,
    get_feishu_webhook,
    is_feishu_bot_enabled,
)

# 初始化
fetcher = FinanceDataFetcher()
processor = DataProcessor(alert_threshold=float(os.getenv('ALERT_THRESHOLD', 3.0)))

alert_webhook = get_feishu_webhook(BOT_ALERT)
report_webhook = get_feishu_webhook(BOT_REPORT)
alert_notifier = FeishuNotifier(alert_webhook) if alert_webhook else None
report_notifier = FeishuNotifier(report_webhook) if report_webhook else None

# 解析配置
STOCK_CODES = os.getenv('STOCK_CODES', '').split(',')
FUND_CODES = os.getenv('FUND_CODES', '').split(',')

def fetch_and_notify():
    """抓取数据并发送通知"""
    print(f"[{datetime.now()}] 开始抓取数据...")
    
    stock_codes = [code.strip() for code in STOCK_CODES if code.strip()]
    fetcher.prefetch_quotes(stock_codes)

    # 获取股票数据
    stocks = []
    for code in stock_codes:
        if code:
            data = fetcher.fetch_stock_data(code)
            if data:
                stocks.append(data)
                
                # 检查是否需要立即提醒（告警机器人）
                if alert_notifier and processor.should_alert(data['change']):
                    alert_notifier.send_market_alert(
                        data['name'], data['code'], 
                        data['price'], data['change'],
                        processor.alert_threshold
                    )
    
    # 获取基金数据
    funds = []
    for code in FUND_CODES:
        if code.strip():
            data = fetcher.fetch_fund_data(code.strip())
            if data:
                funds.append(data)
    
    # 获取大盘指数
    indices = fetcher.fetch_market_index()
    
    # 生成日报
    report = processor.generate_daily_report(stocks, funds, indices)
    
    # 发送日报（日报机器人）
    if report_notifier:
        report_notifier.send_daily_report(report)
    else:
        print("日报机器人未配置或未启用，跳过日报发送")
    
    # 保存历史数据
    all_data = stocks + funds
    processor.save_history(all_data)
    
    print(f"[{datetime.now()}] 数据抓取完成，共 {len(stocks)} 只股票，{len(funds)} 只基金")

def weekly_report_job():
    """周报任务（每周五下午 5 点）"""
    print(f"[{datetime.now()}] 生成周报...")
    # 实现周报逻辑
    # ...

# 定时任务
schedule.every().day.at("09:30").do(fetch_and_notify)  # 每天早上 9:30
schedule.every().friday.at("17:00").do(weekly_report_job)  # 每周五下午 5 点

if __name__ == "__main__":
    print("🤖 财经数据监控助手已启动")
    print(f"监控股票：{STOCK_CODES}")
    print(f"监控基金：{FUND_CODES}")
    print(f"提醒阈值：{processor.alert_threshold}%")
    print(f"告警机器人：{'已启用' if is_feishu_bot_enabled(BOT_ALERT) else '未配置'}")
    print(f"日报机器人：{'已启用' if is_feishu_bot_enabled(BOT_REPORT) else '未配置'}")
    print("按 Ctrl+C 退出\n")
    
    # 立即执行一次
    fetch_and_notify()
    
    # 运行定时任务
    while True:
        schedule.run_pending()
        time.sleep(60)
