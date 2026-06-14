from datetime import datetime, timedelta, date
from typing import Dict, List, Any
import json
import os

class DataProcessor:
    """数据处理与格式化"""
    
    def __init__(self, alert_threshold: float = 3.0):
        self.alert_threshold = alert_threshold
        self.data_dir = 'data/history'
        os.makedirs(self.data_dir, exist_ok=True)
    
    def format_stock_message(self, data: Dict) -> str:
        """格式化股票消息"""
        emoji = '📈' if data['change'] > 0 else '📉' if data['change'] < 0 else '➖'
        
        return (
            f"{emoji} **{data['name']} ({data['code']})**\n"
            f"现价：{data['price']:.2f}\n"
            f"涨跌：{data['change']:+.2f}% ({data['change_amount']:+.2f})\n"
            f"最高：{data.get('high', '-'):.2f}  最低：{data.get('low', '-'):.2f}\n"
            f"成交量：{data.get('volume', 0):,.0f}\n"
        )
    
    def format_fund_message(self, data: Dict) -> str:
        """格式化基金消息"""
        emoji = '📈' if data['change'] > 0 else '📉' if data['change'] < 0 else '➖'
        
        return (
            f"{emoji} **{data['name']} ({data['code']})**\n"
            f"净值：{data['nav']:.4f}\n"
            f"涨跌：{data['change']:+.2f}% ({data['change_amount']:+.4f})\n"
            f"日期：{data['date']}\n"
        )
    
    def should_alert(self, change: float) -> bool:
        """判断是否需要发送提醒"""
        return abs(change) >= self.alert_threshold
    
    def generate_daily_report(self, stocks: List[Dict], funds: List[Dict], 
                              indices: Dict) -> str:
        """生成日报"""
        date = datetime.now().strftime('%Y年%m月%d日')
        
        # 大盘概览
        report = f"📊 **财经日报** ({date})\n\n"
        report += "━━━ 大盘指数 ━━━\n"
        for name, data in indices.items():
            emoji = '🔺' if data['change'] > 0 else '🔻'
            report += f"{emoji} {name}: {data['price']:.2f} ({data['change']:+.2f}%)\n"
        
        # 股票持仓
        if stocks:
            report += "\n━━━ 持仓股票 ━━━\n"
            for stock in stocks:
                report += self.format_stock_message(stock) + "\n"
        
        # 基金持仓
        if funds:
            report += "\n━━━ 持仓基金 ━━━\n"
            for fund in funds:
                report += self.format_fund_message(fund) + "\n"
        
        # 涨跌统计
        all_holdings = stocks + funds
        if all_holdings:
            up_count = sum(1 for item in all_holdings if item.get('change', 0) > 0)
            down_count = sum(1 for item in all_holdings if item.get('change', 0) < 0)
            report += f"\n📊 今日涨跌：{up_count} 涨 | {down_count} 跌\n"
        
        return report
    
    def generate_weekly_report(self, week_data: List[Dict]) -> str:
        """生成周报"""
        date_range = self._get_week_date_range()
        
        report = f"📈 **投资周报** ({date_range})\n\n"
        
        # 周涨跌统计
        if week_data:
            report += "━━━ 本周表现 ━━━\n"
            for item in week_data:
                weekly_change = item.get('weekly_change', 0)
                emoji = '📈' if weekly_change > 0 else '📉'
                report += f"{emoji} {item['name']}: {weekly_change:+.2f}%\n"
        
        # 操作建议
        report += "\n💡 **下周展望**\n"
        report += "- 关注市场成交量变化\n"
        report += "- 注意财报季个股风险\n"
        report += "- 保持合理仓位控制\n"
        
        return report
    
    def _get_week_date_range(self) -> str:
        """获取本周日期范围"""
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)
        return f"{monday.strftime('%m.%d')}-{friday.strftime('%m.%d')}"
    
    def _serialize_for_json(self, obj: Any) -> Any:
        """将 datetime/date 等不可 JSON 序列化的对象转为字符串"""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {key: self._serialize_for_json(value) for key, value in obj.items()}
        if isinstance(obj, list):
            return [self._serialize_for_json(item) for item in obj]
        return obj

    def save_history(self, data: List[Dict]):
        """保存历史数据"""
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f"{self.data_dir}/{date_str}.json"
        
        # 读取已有数据
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except json.JSONDecodeError:
                history = []
        else:
            history = []
        
        # 追加新数据
        history.extend(self._serialize_for_json(item) for item in data)
        
        # 保存
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)