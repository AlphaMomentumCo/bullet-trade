import requests
import json
from typing import List, Dict
from datetime import datetime

class FeishuNotifier:
    """飞书消息通知"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    def send_text(self, content: str):
        """发送文本消息"""
        payload = {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }
        return self._send(payload)
    
    def send_post(self, title: str, content: List[List[Dict]]):
        """发送富文本卡片"""
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": content
                    }
                }
            }
        }
        return self._send(payload)
    
    def send_interactive_card(self, template: Dict):
        """发送交互式卡片"""
        payload = {
            "msg_type": "interactive",
            "card": template
        }
        return self._send(payload)
    
    def _send(self, payload: Dict) -> bool:
        """发送消息"""
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={'Content-Type': 'application/json'}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('StatusCode') == 0 or result.get('code') == 0:
                    return True
                else:
                    print(f"飞书返回错误：{result}")
                    return False
            else:
                print(f"HTTP 错误：{response.status_code}")
                return False
                
        except Exception as e:
            print(f"发送失败：{e}")
            return False
    
    def send_market_alert(self, name: str, code: str, price: float, 
                          change: float, threshold: float):
        """发送市场提醒"""
        emoji = '🚨' if abs(change) >= threshold else '📢'
        color = 'red' if change > 0 else 'green'
        
        content = f"{emoji} **价格提醒**\n\n"
        content += f"**{name}** ({code})\n"
        content += f"现价：{price:.2f}\n"
        content += f"涨跌：{change:+.2f}%\n"
        content += f"超过阈值：{threshold}%"
        
        return self.send_text(content)
    
    def send_daily_report(self, report: str):
        """发送日报"""
        # 使用富文本格式
        lines = report.split('\n')
        content = []
        
        for line in lines:
            if line.strip():
                content.append([{"tag": "text", "text": line + "\n"}])
        
        return self.send_post("财经日报", content)