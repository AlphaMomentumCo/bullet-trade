import re
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import akshare as ak
import requests


class FinanceDataFetcher:
    """财经数据抓取器"""

    SINA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn",
    }

    INDEX_SINA_CODES = {
        "sh000001": "sh000001",
        "sz399001": "sz399001",
        "sz399006": "sz399006",
        "hkHSI": "rt_hkHSI",
        "usIXIC": "gb_$ixic",
        "usINX": "gb_$inx",
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.SINA_HEADERS)
        self.request_count = 0
        self.last_request_time = 0
        self._quote_cache: Dict[str, Dict] = {}
        self._fund_daily_df = None

    def _rate_limit(self):
        """请求限流，避免被数据源封 IP"""
        self.request_count += 1
        current_time = time.time()

        if self.request_count > 60:
            elapsed = current_time - self.last_request_time
            if elapsed < 60:
                time.sleep(60 - elapsed)
            self.request_count = 0

        self.last_request_time = time.time()

    def _retry(self, func: Callable, retries: int = 3, delay: float = 1.0):
        last_error = None
        for attempt in range(retries):
            try:
                return func()
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
        raise last_error

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _to_sina_code(self, code: str) -> str:
        if code in self.INDEX_SINA_CODES:
            return self.INDEX_SINA_CODES[code]

        if code.startswith("us"):
            return f"gb_{code[2:].lower()}"

        return code

    def _fetch_sina_quotes(self, codes: List[str]) -> Dict[str, List[str]]:
        pending = [code for code in codes if code not in self._quote_cache]
        if not pending:
            return {}

        self._rate_limit()
        sina_codes = [self._to_sina_code(code) for code in pending]
        url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"

        def _request():
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            return response.text

        text = self._retry(_request)
        parsed: Dict[str, List[str]] = {}

        for line in text.splitlines():
            match = re.match(r'var hq_str_(?P<sina_code>[^=]+)="(?P<payload>.*)";', line)
            if not match:
                continue

            sina_code = match.group("sina_code")
            payload = match.group("payload")
            if not payload:
                continue

            for original_code, mapped_code in zip(pending, sina_codes):
                if mapped_code == sina_code:
                    parsed[original_code] = payload.split(",")
                    break

        return parsed

    def _parse_a_share(self, code: str, fields: List[str]) -> Optional[Dict]:
        if len(fields) < 6:
            return None

        prev_close = self._safe_float(fields[2])
        price = self._safe_float(fields[3])
        if prev_close <= 0:
            return None

        change_amount = price - prev_close
        change = change_amount / prev_close * 100

        result = {
            "code": code,
            "name": fields[0],
            "price": price,
            "change": round(change, 2),
            "change_amount": round(change_amount, 4),
            "open": self._safe_float(fields[1]),
            "prev_close": prev_close,
            "high": self._safe_float(fields[4]),
            "low": self._safe_float(fields[5]),
            "timestamp": datetime.now(),
        }

        if len(fields) > 9:
            result["volume"] = self._safe_float(fields[8])
            result["amount"] = self._safe_float(fields[9])

        return result

    def _parse_hk_stock(self, code: str, fields: List[str]) -> Optional[Dict]:
        if len(fields) < 10:
            return None

        prev_close = self._safe_float(fields[3])
        price = self._safe_float(fields[9] or fields[6])
        if prev_close <= 0:
            return None

        change_amount = price - prev_close
        change = change_amount / prev_close * 100

        return {
            "code": code,
            "name": fields[1] or fields[0],
            "price": price,
            "change": round(change, 2),
            "change_amount": round(change_amount, 4),
            "open": self._safe_float(fields[2]),
            "prev_close": prev_close,
            "high": self._safe_float(fields[4]),
            "low": self._safe_float(fields[5]),
            "volume": self._safe_float(fields[11]) if len(fields) > 11 else 0.0,
            "timestamp": datetime.now(),
        }

    def _parse_us_stock(self, code: str, fields: List[str]) -> Optional[Dict]:
        if len(fields) < 5:
            return None

        price = self._safe_float(fields[1])
        return {
            "code": code,
            "name": fields[0],
            "price": price,
            "change": round(self._safe_float(fields[2]), 2),
            "change_amount": round(self._safe_float(fields[4]), 4),
            "open": self._safe_float(fields[5]) if len(fields) > 5 else 0.0,
            "high": self._safe_float(fields[6]) if len(fields) > 6 else 0.0,
            "low": self._safe_float(fields[7]) if len(fields) > 7 else 0.0,
            "volume": self._safe_float(fields[10]) if len(fields) > 10 else 0.0,
            "timestamp": datetime.now(),
        }

    def _parse_hk_index(self, code: str, fields: List[str]) -> Optional[Dict]:
        if len(fields) < 9:
            return None

        return {
            "code": code,
            "name": fields[1],
            "price": self._safe_float(fields[6]),
            "change": round(self._safe_float(fields[8]), 2),
            "change_amount": round(self._safe_float(fields[7]), 4),
            "timestamp": datetime.now(),
        }

    def _parse_us_index(self, code: str, fields: List[str]) -> Optional[Dict]:
        if len(fields) < 5:
            return None

        return {
            "code": code,
            "name": fields[0],
            "price": self._safe_float(fields[1]),
            "change": round(self._safe_float(fields[2]), 2),
            "change_amount": round(self._safe_float(fields[4]), 4),
            "timestamp": datetime.now(),
        }

    def _parse_quote(self, code: str, fields: List[str]) -> Optional[Dict]:
        if code.startswith("us") and code in self.INDEX_SINA_CODES:
            return self._parse_us_index(code, fields)
        if code == "hkHSI":
            return self._parse_hk_index(code, fields)
        if code.startswith("hk"):
            return self._parse_hk_stock(code, fields)
        if code.startswith("us"):
            return self._parse_us_stock(code, fields)
        return self._parse_a_share(code, fields)

    def _get_quote(self, code: str) -> Optional[Dict]:
        if code not in self._quote_cache:
            quotes = self._fetch_sina_quotes([code])
            fields = quotes.get(code)
            if not fields:
                return None
            parsed = self._parse_quote(code, fields)
            if parsed:
                self._quote_cache[code] = parsed
            return parsed

        return self._quote_cache[code]

    def prefetch_quotes(self, codes: List[str]):
        """批量预取行情，减少重复请求"""
        missing = [code for code in codes if code and code not in self._quote_cache]
        if not missing:
            return

        quotes = self._fetch_sina_quotes(missing)
        for code, fields in quotes.items():
            parsed = self._parse_quote(code, fields)
            if parsed:
                self._quote_cache[code] = parsed

    def fetch_stock_data(self, code: str) -> Optional[Dict]:
        """
        获取股票数据
        code: 股票代码，如 sh600519, sz000001, hk00700, usAAPL
        """
        try:
            return self._get_quote(code)
        except Exception as exc:
            print(f"获取股票 {code} 失败：{exc}")
            return None

    def _load_fund_daily(self):
        if self._fund_daily_df is not None:
            return

        def _request():
            return ak.fund_open_fund_daily_em()

        self._fund_daily_df = self._retry(_request)

    def fetch_fund_data(self, code: str) -> Optional[Dict]:
        """
        获取基金数据
        code: 基金代码，如 000001
        """
        try:
            self._load_fund_daily()
            fund_row = self._fund_daily_df[self._fund_daily_df["基金代码"] == code]
            if not fund_row.empty:
                row = fund_row.iloc[0]
                nav_columns = [
                    column
                    for column in row.index
                    if column.endswith("-单位净值") and row[column] not in ("", None)
                ]
                if nav_columns:
                    nav_columns.sort(reverse=True)
                    latest_nav = self._safe_float(row[nav_columns[0]])
                    change_amount = self._safe_float(row.get("日增长值", 0))
                    change = self._safe_float(row.get("日增长率", 0))
                    prev_nav = latest_nav - change_amount if latest_nav else 0.0

                    return {
                        "code": code,
                        "name": row["基金简称"],
                        "nav": latest_nav,
                        "change": round(change, 2),
                        "change_amount": round(change_amount, 4),
                        "date": nav_columns[0].split("-单位净值")[0],
                        "timestamp": datetime.now(),
                    }

            df = self._retry(
                lambda: ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
            )
            if df.empty:
                return None

            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            nav = self._safe_float(latest["单位净值"])
            prev_nav = self._safe_float(prev["单位净值"])
            change = ((nav - prev_nav) / prev_nav * 100) if prev_nav else 0.0

            return {
                "code": code,
                "name": code,
                "nav": nav,
                "change": round(change, 2),
                "change_amount": round(nav - prev_nav, 4),
                "date": str(latest["净值日期"]),
                "timestamp": datetime.now(),
            }
        except Exception as exc:
            print(f"获取基金 {code} 失败：{exc}")
            return None

    def fetch_market_index(self) -> Dict:
        """获取大盘指数"""
        indices = {
            "上证指数": "sh000001",
            "深证成指": "sz399001",
            "创业板指": "sz399006",
            "恒生指数": "hkHSI",
            "纳斯达克": "usIXIC",
            "标普 500": "usINX",
        }

        codes = list(indices.values())
        try:
            self.prefetch_quotes(codes)
        except Exception as exc:
            print(f"获取大盘指数失败：{exc}")
            return {}

        result = {}
        for name, code in indices.items():
            data = self._quote_cache.get(code) or self._get_quote(code)
            if data:
                result[name] = {
                    "price": data["price"],
                    "change": data["change"],
                }

        return result
