from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as Date
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd

from .base import DataProvider
from ..cache import CacheManager


class TushareProvider(DataProvider):
    """基于 tushare.pro 的数据提供者，字段与复权口径对齐兼容层约定。"""

    name: str = "tushare"
    _TS_SUFFIX_TO_JQ = {"SH": "XSHG", "SZ": "XSHE"}
    _JQ_SUFFIX_TO_TS = {"XSHG": "SH", "XSHE": "SZ"}

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self._token = self.config.get("token") or os.getenv("TUSHARE_TOKEN")
        self._tushare_custom_url = self.config.get("tushare_custom_url") or os.getenv("TUSHARE_CUSTOM_URL")
        cache_dir_set = "cache_dir" in self.config
        cache_dir = self.config.get("cache_dir")
        if isinstance(cache_dir, str) and cache_dir:
            cache_dir = os.path.expanduser(cache_dir)
        self._cache = CacheManager(
            provider_name=self.name,
            cache_dir=cache_dir,
            fallback_to_env=not cache_dir_set,
        )
        self._pro = None
        self._asset_type_cache: Dict[str, str] = {}
        # 进程内记忆缓存：即使关闭磁盘缓存也能避免重复打网（回测友好）
        # bench 可设 mem_cache=False 测冷启动
        mem_cfg = self.config.get("mem_cache")
        if mem_cfg is None:
            mem_cfg = os.getenv("TUSHARE_MEM_CACHE", "1") not in ("0", "false", "False", "")
        self._mem_cache_enabled = bool(mem_cfg)
        self._mem: Dict[str, Any] = {}
        self._tick_src = self.config.get("tick_src") or os.getenv("TUSHARE_TICK_SRC", "tt")

    # ------------------------ 公共工具 ------------------------
    @classmethod
    def _to_ts_code(cls, security: str) -> str:
        if not security or not isinstance(security, str) or "." not in security:
            return security
        code, suffix = security.split(".", 1)
        mapped = cls._JQ_SUFFIX_TO_TS.get(suffix.upper())
        if mapped:
            return f"{code}.{mapped}"
        return security

    @classmethod
    def _to_jq_code(cls, security: str) -> str:
        if not security or not isinstance(security, str) or "." not in security:
            return security
        code, suffix = security.split(".", 1)
        mapped = cls._TS_SUFFIX_TO_JQ.get(suffix.upper())
        if mapped:
            return f"{code}.{mapped}"
        return security

    @staticmethod
    def _ensure_ts_module():
        try:
            import tushare as ts  # type: ignore

            return ts
        except ImportError as exc:  # pragma: no cover - 仅在缺失依赖时触发
            raise ImportError(
                "未安装 tushare，请执行 `pip install bullet-trade[tushare]` 或 `pip install tushare`"
            ) from exc

    def _ensure_client(self):
        if self._pro is None:
            self.auth()
        return self._pro

    def _memo_get(self, key: str) -> Tuple[bool, Any]:
        if not self._mem_cache_enabled:
            return False, None
        if key in self._mem:
            return True, self._mem[key]
        return False, None

    def _memo_set(self, key: str, value: Any) -> Any:
        if self._mem_cache_enabled:
            self._mem[key] = value
        return value

    def _memo_call(self, key: str, fetch_fn: Callable[[], Any]) -> Any:
        hit, value = self._memo_get(key)
        if hit:
            return value
        return self._memo_set(key, fetch_fn())

    def clear_mem_cache(self) -> None:
        self._mem.clear()
        self._asset_type_cache.clear()

    def _format_date(self, value: Optional[Union[str, datetime, Date]]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            if len(value) == 8 and value.isdigit():
                return value
            return pd.to_datetime(value).strftime("%Y%m%d")
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, Date):
            return value.strftime("%Y%m%d")
        return None

    @staticmethod
    def _format_date_dash(value: Optional[Union[str, datetime, Date]]) -> Optional[str]:
        if value is None:
            return None
        return pd.to_datetime(value).strftime("%Y-%m-%d")

    def _estimate_start_for_count(
        self,
        end_date: Optional[Union[str, datetime, Date]],
        count: int,
        frequency: str,
    ) -> str:
        """按 count 估算 start_date，避免 pro_bar/daily 拉全历史。"""
        end_dt = pd.to_datetime(end_date) if end_date is not None else pd.Timestamp.today()
        freq = self._normalize_frequency(frequency)
        if self._is_minute_frequency(freq):
            # 约 240 根/日；再留周末缓冲
            cal_days = max(int(count / 200) + 3, 5)
        else:
            cal_days = max(int(count * 2.2) + 8, 15)
        return (end_dt - pd.Timedelta(days=cal_days)).strftime("%Y%m%d")

    def _normalize_frequency(self, frequency: str) -> str:
        freq = frequency.lower()
        if freq in ("daily", "1d", "d"):
            return "D"
        if freq in ("minute", "1m", "m1", "1min"):
            return "1min"
        if freq.endswith("min"):
            return freq
        if freq.endswith("m") and freq[:-1].isdigit():
            return f"{int(freq[:-1])}min"
        if freq.endswith("m"):
            return f"{freq}"
        return freq.upper()

    @staticmethod
    def _is_minute_frequency(freq: str) -> bool:
        return "min" in str(freq).lower()

    def _normalize_price_units(self, df: pd.DataFrame, freq: str, asset: Optional[str]) -> pd.DataFrame:
        """
        统一到聚宽兼容口径：volume=股，money=元。

        Tushare 的日/周/月线 A 股、指数、基金行情使用 volume=手、money=千元；
        股票分钟线 stk_mins 已经是 volume=股、money=元，不需要转换。
        """
        if self._is_minute_frequency(freq):
            return df
        if asset not in {"E", "I", "FD"}:
            return df
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype(float) * 100.0
        if "money" in df.columns:
            df["money"] = pd.to_numeric(df["money"], errors="coerce").astype(float) * 1000.0
        return df

    def _apply_fields(self, df: pd.DataFrame, fields: Optional[List[str]]) -> pd.DataFrame:
        if fields:
            missing = [f for f in fields if f not in df.columns]
            if missing:
                extra_cols = {f: 0.0 for f in missing}
                df = df.assign(**extra_cols)
            df = df[fields]
        return df

    @classmethod
    def _infer_asset_by_code(cls, security: str) -> Optional[str]:
        jq_code = cls._to_jq_code(security)
        if not jq_code or "." not in jq_code:
            return None

        code, suffix = jq_code.split(".", 1)
        suffix = suffix.upper()

        if suffix == "XSHG":
            if code.startswith("000"):
                return "I"
            if code.startswith("5"):
                return "FD"
            if code.startswith("6"):
                return "E"
        elif suffix == "XSHE":
            if code.startswith("399"):
                return "I"
            if code.startswith(("15", "16", "18")):
                return "FD"
            if code.startswith(("000", "001", "002", "003", "300", "301")):
                return "E"

        return None

    def _infer_asset_from_catalog(self, jq_code: str) -> Optional[str]:
        for types, asset in (
            (["index"], "I"),
            (["fund", "etf", "lof"], "FD"),
            (["stock"], "E"),
        ):
            try:
                df = self.get_all_securities(types=types)
            except Exception:
                continue
            if df is not None and not df.empty and jq_code in df.index:
                return asset
        return None

    def _infer_asset(self, security: str) -> str:
        jq_code = self._to_jq_code(security)
        cache_key = jq_code.upper() if isinstance(jq_code, str) else str(jq_code)
        cached = self._asset_type_cache.get(cache_key)
        if cached:
            return cached

        asset = self._infer_asset_by_code(jq_code)
        if asset is None:
            asset = self._infer_asset_from_catalog(jq_code)
        if asset is None:
            asset = "E"

        self._asset_type_cache[cache_key] = asset
        return asset

    # ------------------------ 认证 ------------------------
    def auth(
        self,
        user: Optional[str] = None,
        pwd: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        _ = port, pwd  # tushare 不使用这些字段
        token = user or self._token
        if not token:
            raise RuntimeError("Tushare token 未配置，请设置 TUSHARE_TOKEN 或在 auth 中手动传入")
        ts = self._ensure_ts_module()
        self._pro = ts.pro_api(token)
        self._token = token
        # 支持自定义 API URL（去掉首尾空白与误粘贴引号）
        tushare_custom_url = host or self._tushare_custom_url
        if tushare_custom_url:
            tushare_custom_url = str(tushare_custom_url).strip().strip('"').strip("'")
            self._pro._DataApi__http_url = tushare_custom_url
            print("使用自定义的URL")
    # ------------------------ K 线数据 ------------------------
    def get_price(
        self,
        security: Union[str, List[str]],
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        frequency: str = "daily",
        fields: Optional[List[str]] = None,
        skip_paused: bool = False,
        fq: str = "pre",
        count: Optional[int] = None,
        panel: bool = True,
        fill_paused: bool = True,
        pre_factor_ref_date: Optional[Union[str, datetime]] = None,
        prefer_engine: bool = False,
        force_no_engine: bool = False,
    ) -> pd.DataFrame:
        securities = security if isinstance(security, (list, tuple)) else [security]
        frames: Dict[str, pd.DataFrame] = {}

        for sec in securities:
            asset = self._infer_asset(sec)
            kwargs = {
                "security": sec,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "fields": fields,
                "skip_paused": skip_paused,
                "fq": fq,
                "count": count,
                "pre_factor_ref_date": pre_factor_ref_date,
                "asset": asset,
            }

            def _fetch_single(kw: Dict[str, Any]) -> pd.DataFrame:
                return self._get_price_single(
                    kw["security"],
                    start_date=kw.get("start_date"),
                    end_date=kw.get("end_date"),
                    frequency=kw.get("frequency", "daily"),
                    fields=kw.get("fields"),
                    skip_paused=kw.get("skip_paused", False),
                    fq=kw.get("fq"),
                    count=kw.get("count"),
                    pre_factor_ref_date=kw.get("pre_factor_ref_date"),
                    asset=kw.get("asset"),
                )

            frames[sec] = self._cache.cached_call("get_price", kwargs, _fetch_single, result_type="df")

        if len(frames) == 1:
            return next(iter(frames.values()))

        if panel:
            return pd.concat(frames, axis=1)

        long_rows = []
        for sec, df in frames.items():
            tmp = df.copy()
            tmp["code"] = sec
            long_rows.append(tmp)
        merged = pd.concat(long_rows, axis=0)
        return merged

    def _fetch_ohlcv_raw(
        self,
        ts_code: str,
        asset: str,
        start_str: Optional[str],
        end_str: Optional[str],
        freq: str,
        fq: Optional[str],
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> Tuple[pd.DataFrame, bool]:
        """
        拉取未复权/原始 OHLCV。
        返回 (df, already_qfq)。
        日线股票前复权：优先 daily（代理上显著快于 pro_bar adj=qfq），复权交给本地 adj_factor。
        """
        ts = self._ensure_ts_module()
        pro = self._ensure_client()
        _ = fq, pre_factor_ref_date

        if freq == "D" and asset == "E":
            df = pro.daily(ts_code=ts_code, start_date=start_str, end_date=end_str)
            return (df if df is not None else pd.DataFrame()), False
        if freq == "D" and asset == "I":
            df = pro.index_daily(ts_code=ts_code, start_date=start_str, end_date=end_str)
            return (df if df is not None else pd.DataFrame()), False
        if freq == "D" and asset == "FD":
            try:
                df = pro.fund_daily(ts_code=ts_code, start_date=start_str, end_date=end_str)
                if df is not None and not df.empty:
                    return df, False
            except Exception:
                pass

        df = ts.pro_bar(
            ts_code=ts_code,
            start_date=start_str,
            end_date=end_str,
            freq=freq,
            adj=None,
            asset=asset,
            api=pro,
        )
        return (df if df is not None else pd.DataFrame()), False

    def _get_price_single(
        self,
        security: str,
        start_date: Optional[Union[str, datetime]],
        end_date: Optional[Union[str, datetime]],
        frequency: str,
        fields: Optional[List[str]],
        skip_paused: bool,
        fq: Optional[str],
        count: Optional[int],
        pre_factor_ref_date: Optional[Union[str, datetime]],
        asset: Optional[str] = None,
    ) -> pd.DataFrame:
        end_str = self._format_date(end_date)
        freq = self._normalize_frequency(frequency)
        asset = asset or self._infer_asset(security)
        ts_code = self._to_ts_code(security)

        start_str = self._format_date(start_date)
        if count and not start_str:
            start_str = self._estimate_start_for_count(end_date or end_str, int(count), frequency)

        mem_key = (
            f"price:{ts_code}:{start_str}:{end_str}:{freq}:{fq}:{count}:"
            f"{self._format_date(pre_factor_ref_date)}:{skip_paused}:{tuple(fields or ())}"
        )
        hit, cached = self._memo_get(mem_key)
        if hit:
            return cached.copy() if isinstance(cached, pd.DataFrame) else cached

        need_adj = asset == "E" and fq in ("pre", "post")
        # 日线前复权：daily 与 adj_factor 并行，避免串行双 RTT
        factor_df = None
        if need_adj and freq == "D":
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_px = pool.submit(
                        self._fetch_ohlcv_raw,
                        ts_code,
                        asset,
                        start_str,
                        end_str,
                        freq,
                        fq,
                        pre_factor_ref_date,
                    )
                    # adj 窗口先按请求区间取；空数据时后面再兜底
                    start_for_adj = start_str or end_str
                    end_for_adj = end_str or start_str
                    fut_adj = pool.submit(
                        self._fetch_adj_factor,
                        security,
                        pd.to_datetime(start_for_adj),
                        pd.to_datetime(end_for_adj),
                    )
                    df, already_qfq = fut_px.result()
                    factor_df = fut_adj.result()
            except Exception:
                df, already_qfq = self._fetch_ohlcv_raw(
                    ts_code, asset, start_str, end_str, freq, fq, pre_factor_ref_date
                )
                factor_df = None
        else:
            df, already_qfq = self._fetch_ohlcv_raw(
                ts_code, asset, start_str, end_str, freq, fq, pre_factor_ref_date
            )

        if df is None or df.empty:
            return self._memo_set(mem_key, pd.DataFrame())

        time_col = "trade_time" if self._is_minute_frequency(freq) and "trade_time" in df.columns else "trade_date"
        if time_col not in df.columns:
            for cand in ("trade_time", "trade_date", "datetime"):
                if cand in df.columns:
                    time_col = cand
                    break
        df = df.sort_values(time_col)
        df.index = pd.to_datetime(df[time_col])
        df.rename(
            columns={
                "vol": "volume",
                "amount": "money",
                "high_limit": "high_limit",
                "low_limit": "low_limit",
            },
            inplace=True,
        )
        if "ts_code" in df.columns:
            df["ts_code"] = df["ts_code"].map(self._to_jq_code)
        df["money"] = df.get("money", 0.0)
        df["volume"] = df.get("volume", 0.0)
        df = self._normalize_price_units(df, freq, asset)

        if skip_paused and "is_paused" in df.columns:
            df = df[df["is_paused"] == 0]

        if need_adj and not already_qfq:
            if factor_df is not None and not factor_df.empty and "adj_factor" in factor_df.columns:
                df = self._apply_adjustment_with_factor(
                    df, factor_df, fq=fq, pre_factor_ref_date=pre_factor_ref_date
                )
            else:
                df = self._apply_adjustment(
                    security=security,
                    df=df,
                    fq=fq,
                    pre_factor_ref_date=pre_factor_ref_date,
                )

        if count:
            df = df.tail(count)

        df = self._apply_fields(df, fields)
        return self._memo_set(mem_key, df)

    def _apply_adjustment_with_factor(
        self,
        df: pd.DataFrame,
        factor_df: pd.DataFrame,
        fq: str,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> pd.DataFrame:
        """用已取回的 adj_factor 做复权（避免重复打网）。"""
        if factor_df is None or factor_df.empty or "adj_factor" not in factor_df.columns:
            return df
        start_dt = df.index.min()
        end_dt = df.index.max()
        factor_df = factor_df.copy()
        factor_df.index = pd.to_datetime(factor_df["trade_date"]).dt.normalize()
        merged = df.copy()
        merged["_factor_date"] = pd.to_datetime(merged.index).normalize()
        merged = merged.join(factor_df["adj_factor"], on="_factor_date", how="left")
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        ref_date = pre_factor_ref_date
        if ref_date is None and fq == "pre":
            # 用区间末日作基准，避免再打 get_trade_days
            ref_date = end_dt
        if ref_date is None:
            ref_date = end_dt if fq == "pre" else start_dt
        try:
            ref_dt = pd.to_datetime(ref_date).normalize()
        except Exception:
            ref_dt = pd.to_datetime(end_dt if fq == "pre" else start_dt).normalize()
        if ref_dt in factor_df.index:
            ref_factor = factor_df.loc[ref_dt, "adj_factor"]
        else:
            ref_factor = merged["adj_factor"].iloc[-1] if fq == "pre" else merged["adj_factor"].iloc[0]
        if ref_factor is None or (isinstance(ref_factor, float) and pd.isna(ref_factor)):
            return df
        ratio = (merged["adj_factor"] / ref_factor) if fq == "pre" else (ref_factor / merged["adj_factor"])
        for col in ["open", "high", "low", "close"]:
            if col in merged.columns:
                merged[col] = merged[col] * ratio
        merged.drop(columns=["adj_factor", "_factor_date"], inplace=True, errors="ignore")
        return merged

    def _apply_adjustment(
        self,
        security: str,
        df: pd.DataFrame,
        fq: str,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> pd.DataFrame:
        start_dt = df.index.min()
        end_dt = df.index.max()
        factor_df = self._fetch_adj_factor(security, start_dt, end_dt)
        if factor_df.empty or "adj_factor" not in factor_df.columns:
            fallback = self._build_adjusted_from_events(
                security=security,
                raw_df=df,
                fq=fq,
                pre_factor_ref_date=pre_factor_ref_date,
            )
            return fallback if not fallback.empty else df
        return self._apply_adjustment_with_factor(df, factor_df, fq=fq, pre_factor_ref_date=pre_factor_ref_date)

    def _build_adjusted_from_events(
        self,
        security: str,
        raw_df: pd.DataFrame,
        fq: str,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> pd.DataFrame:
        if fq not in ("pre", "post"):
            return pd.DataFrame()
        if raw_df.empty:
            return pd.DataFrame()
        if fq == "post":
            return pd.DataFrame()

        def _to_date(value: Optional[Union[str, datetime, Date]]) -> Optional[Date]:
            if value is None:
                return None
            try:
                return pd.to_datetime(value).date()
            except Exception:
                return None

        start_dt = raw_df.index.min()
        end_dt = raw_df.index.max()
        ref_date: Optional[Union[str, datetime, Date]] = None
        if fq == "pre":
            if pre_factor_ref_date is not None:
                ref_date = pre_factor_ref_date
            else:
                latest_trade_day = self._latest_trade_day()
                ref_date = latest_trade_day.date() if isinstance(latest_trade_day, datetime) else latest_trade_day
                if ref_date is None:
                    ref_date = Date.today()

        start_date = _to_date(start_dt)
        end_date = _to_date(ref_date if ref_date is not None else end_dt)
        if start_date and end_date and end_date < start_date:
            end_date = start_date

        events = self.get_split_dividend(
            security,
            start_date=start_date,
            end_date=end_date,
        )
        if not events:
            return pd.DataFrame()

        price_cols = [col for col in ["open", "high", "low", "close"] if col in raw_df.columns]
        if not price_cols:
            return pd.DataFrame()

        adj_df = raw_df.copy()
        factors = pd.Series(1.0, index=adj_df.index)
        # 基于分红/送转事件构建前复权因子
        sorted_events = sorted(
            (
                {
                    **event,
                    "date": pd.to_datetime(event.get("date"), errors="coerce"),
                }
                for event in events
            ),
            key=lambda item: item["date"] if item["date"] is not pd.NaT else pd.Timestamp.max,
        )
        for event in sorted_events:
            event_date = event.get("date")
            if event_date is pd.NaT or event_date is None:
                continue
            event_day = event_date.date()
            mask = adj_df.index.date < event_day
            if not mask.any():
                continue
            try:
                scale = float(event.get("scale_factor") or 1.0)
            except Exception:
                scale = 1.0
            scale_factor = 1.0 / scale if scale and scale > 0 else 1.0
            try:
                cash = float(event.get("bonus_pre_tax") or 0.0)
            except Exception:
                cash = 0.0
            try:
                per_base = float(event.get("per_base") or 10.0)
            except Exception:
                per_base = 10.0
            cash_per_share = cash / per_base if per_base > 0 else 0.0

            preclose = None
            if "pre_close" in adj_df.columns and event_day in adj_df.index.date:
                preclose = float(adj_df.loc[adj_df.index.date == event_day, "pre_close"].iloc[0])
            elif "preClose" in adj_df.columns and event_day in adj_df.index.date:
                preclose = float(adj_df.loc[adj_df.index.date == event_day, "preClose"].iloc[0])
            if preclose is None or preclose == 0.0:
                prev = adj_df.index[adj_df.index.date < event_day]
                if len(prev) > 0 and "close" in adj_df.columns:
                    preclose = float(adj_df.loc[prev.max(), "close"])

            cash_factor = 1.0
            if cash_per_share and preclose and preclose > 0:
                cash_factor = max((preclose - cash_per_share) / preclose, 0.0)
            total_factor = scale_factor * cash_factor
            if total_factor != 1.0:
                factors.loc[mask] = factors.loc[mask] * total_factor

        for col in price_cols:
            adj_df[col] = adj_df[col].astype(float) * factors

        return self._align_reference(raw_df, adj_df, pre_factor_ref_date)

    @staticmethod
    def _align_reference(
        raw_df: pd.DataFrame,
        adj_df: pd.DataFrame,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> pd.DataFrame:
        if adj_df.empty or not pre_factor_ref_date:
            return adj_df
        try:
            ref_dt = pd.to_datetime(pre_factor_ref_date)
        except Exception:
            return adj_df
        if ref_dt not in raw_df.index or ref_dt not in adj_df.index:
            return adj_df
        try:
            reference_raw = float(raw_df.loc[ref_dt, "close"])
            reference_adj = float(adj_df.loc[ref_dt, "close"])
        except Exception:
            return adj_df
        if reference_adj == 0.0:
            return adj_df
        scale = reference_raw / reference_adj
        for col in ["open", "high", "low", "close"]:
            if col in adj_df.columns:
                adj_df[col] = adj_df[col] * scale
        return adj_df

    def _latest_trade_day(self) -> Optional[datetime]:
        try:
            days = self.get_trade_days(end_date=Date.today(), count=1)
            if days:
                return pd.to_datetime(days[-1])
        except Exception:
            return None
        return None

    def _fetch_adj_factor(self, security: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        kwargs = {
            "security": security,
            "start_date": start_dt.strftime("%Y%m%d"),
            "end_date": end_dt.strftime("%Y%m%d"),
        }

        def _fetch(kw: Dict[str, Any]) -> pd.DataFrame:
            pro = self._ensure_client()
            ts_code = self._to_ts_code(kw["security"])
            return pro.adj_factor(
                ts_code=ts_code,
                start_date=kw["start_date"],
                end_date=kw["end_date"],
            )

        return self._cache.cached_call("adj_factor", kwargs, _fetch, result_type="df")

    # ------------------------ 交易日/基础信息 ------------------------
    def get_trade_days(
        self,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        count: Optional[int] = None,
    ) -> List[datetime]:
        kwargs = {
            "start_date": start_date,
            "end_date": end_date,
            "count": count,
        }

        def _fetch(kw: Dict[str, Any]) -> List[str]:
            pro = self._ensure_client()
            start = self._format_date(kw.get("start_date"))
            end = self._format_date(kw.get("end_date"))
            count = kw.get("count")
            # 仅 count+end 时补 start，避免 trade_cal 拉全历史
            if count and end and not start and count != -1:
                end_dt = pd.to_datetime(end)
                start = (end_dt - pd.Timedelta(days=int(count) * 3 + 20)).strftime("%Y%m%d")
            df = pro.trade_cal(
                exchange="SSE",
                start_date=start,
                end_date=end,
                fields="cal_date,is_open",
            )
            open_days = df[df["is_open"] == 1]["cal_date"].sort_values().tolist()
            if count and count != -1:
                open_days = open_days[-int(count) :]
            return open_days

        memo_key = f"trade_days:{kwargs.get('start_date')}:{kwargs.get('end_date')}:{kwargs.get('count')}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            date_strs = cached
        else:
            date_strs = self._cache.cached_call("get_trade_days", kwargs, _fetch, result_type="list_str")
            self._memo_set(memo_key, date_strs)
        return [pd.to_datetime(d).to_pydatetime() for d in date_strs]

    def get_all_securities(
        self,
        types: Union[str, List[str]] = "stock",
        date: Optional[Union[str, datetime]] = None,
    ) -> pd.DataFrame:
        if isinstance(types, str):
            types = [types]

        kwargs = {"types": tuple(sorted(types)), "date": date}

        def _fetch(kw: Dict[str, Any]) -> Dict[str, Any]:
            pro = self._ensure_client()
            rows = []
            for t in kw["types"]:
                if t == "stock":
                    df = pro.stock_basic(
                        exchange="",
                        list_status="L",
                        fields="ts_code,name,list_date,delist_date",
                    )
                    df["type"] = "stock"
                elif t in ("fund", "etf", "lof"):
                    df = pro.fund_basic(
                        status="L",
                        market="E",
                        fields="ts_code,name,list_date,delist_date,found_date",
                    )
                    if t == "etf":
                        df = df[df["ts_code"].str.endswith(("SH", "SZ"))]
                    elif t == "lof":
                        df = df[df["ts_code"].str.contains("LOF")]
                    df["type"] = t
                elif t == "index":
                    df = pro.index_basic(market="SSE")
                    df = pd.concat([df, pro.index_basic(market="SZSE")])
                    df.rename(columns={"fullname": "name"}, inplace=True)
                    df["type"] = "index"
                else:
                    continue

                df["display_name"] = df["name"]
                if "list_date" in df.columns:
                    start_series = df["list_date"]
                elif "found_date" in df.columns:
                    start_series = df["found_date"]
                else:
                    start_series = pd.Series([None] * len(df))
                df["start_date"] = pd.to_datetime(start_series, errors="coerce")
                end_series = df["delist_date"] if "delist_date" in df.columns else pd.Series([None] * len(df))
                df["end_date"] = pd.to_datetime(end_series, errors="coerce")
                rows.append(df[["ts_code", "display_name", "name", "start_date", "end_date", "type"]])

            if not rows:
                return {}
            merged = pd.concat(rows, ignore_index=True).drop_duplicates("ts_code")
            if kw.get("date") is not None:
                try:
                    target_dt = pd.to_datetime(kw["date"])
                    start_dt = pd.to_datetime(merged["start_date"], errors="coerce").fillna(pd.Timestamp.min)
                    end_dt = pd.to_datetime(merged["end_date"], errors="coerce").fillna(pd.Timestamp.max)
                    merged = merged[(start_dt <= target_dt) & (end_dt >= target_dt)]
                except Exception:
                    pass
            merged.set_index("ts_code", inplace=True)
            merged.index = [self._to_jq_code(code) for code in merged.index]
            return merged.to_dict(orient="index")

        data = self._cache.cached_call("get_all_securities", kwargs, _fetch, result_type="list_dict")
        if not data:
            return pd.DataFrame(columns=["display_name", "name", "start_date", "end_date", "type"])
        df = pd.DataFrame.from_dict(data, orient="index")
        df["start_date"] = pd.to_datetime(df["start_date"])
        df["end_date"] = pd.to_datetime(df["end_date"])
        return df

    def _fetch_index_weight_df(self, index_code: str, date: Optional[Union[str, datetime]] = None) -> pd.DataFrame:
        """
        拉取指数权重。权重多为月末更新，单日 trade_date 常空；
        直接一次区间查询取最近一期，避免「空查 + 再查」双 RTT。
        """
        target_date = self._format_date(date) or datetime.today().strftime("%Y%m%d")
        memo_key = f"idx_weight:{index_code}:{target_date}"

        def _fetch() -> pd.DataFrame:
            pro = self._ensure_client()
            end_dt = pd.to_datetime(target_date)
            # 覆盖至少一个调仓月；绝大多数一次命中
            start_dt = end_dt - pd.Timedelta(days=40)
            df = pro.index_weight(
                index_code=index_code,
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=target_date,
            )
            if df is None or df.empty:
                start_dt = end_dt - pd.Timedelta(days=120)
                df = pro.index_weight(
                    index_code=index_code,
                    start_date=start_dt.strftime("%Y%m%d"),
                    end_date=target_date,
                )
            if df is None or df.empty:
                return pd.DataFrame()
            latest = df["trade_date"].max()
            return df[df["trade_date"] == latest].copy()

        return self._memo_call(memo_key, _fetch)

    def _stock_basic_row(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """一次性缓存全市场 stock_basic，点查走本地 dict（显著加速 security_info/industry）。"""

        def _load() -> Dict[str, Dict[str, Any]]:
            pro = self._ensure_client()
            df = pro.stock_basic(
                exchange="",
                list_status="L",
                fields="ts_code,name,list_date,delist_date,industry,market",
            )
            if df is None or df.empty:
                return {}
            out: Dict[str, Dict[str, Any]] = {}
            for row in df.itertuples(index=False):
                out[str(row.ts_code)] = {
                    "ts_code": str(row.ts_code),
                    "name": getattr(row, "name", None),
                    "list_date": getattr(row, "list_date", None),
                    "delist_date": getattr(row, "delist_date", None),
                    "industry": getattr(row, "industry", None),
                    "market": getattr(row, "market", None),
                }
            return out

        mapping = self._memo_call("stock_basic_map_L", _load)
        return mapping.get(ts_code)

    def get_index_stocks(self, index_symbol: str, date: Optional[Union[str, datetime]] = None) -> List[str]:
        kwargs = {"index_symbol": index_symbol, "date": date}

        def _fetch(kw: Dict[str, Any]) -> List[str]:
            index_code = self._to_ts_code(kw["index_symbol"])
            df = self._fetch_index_weight_df(index_code, kw.get("date"))
            if df is None or df.empty:
                return []
            return [self._to_jq_code(code) for code in df["con_code"].dropna().tolist()]

        return self._cache.cached_call("get_index_stocks", kwargs, _fetch, result_type="list_str")

    def get_index_weights(self, index_id: str, date: Optional[Union[str, datetime]] = None) -> Any:
        kwargs = {"index_id": index_id, "date": date}

        def _fetch(kw: Dict[str, Any]) -> pd.DataFrame:
            index_code = self._to_ts_code(kw["index_id"])
            df = self._fetch_index_weight_df(index_code, kw.get("date"))
            if df is None or df.empty:
                return pd.DataFrame(columns=["code", "weight", "date"])
            df = df.rename(columns={"con_code": "code", "trade_date": "date"})
            df["code"] = df["code"].map(self._to_jq_code)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            return df[["code", "weight", "date"]]

        return self._cache.cached_call("get_index_weights", kwargs, _fetch, result_type="df")

    def get_security_info(
        self,
        security: str,
        date: Optional[Union[str, datetime]] = None,
    ) -> Dict[str, Any]:
        def _normalize_date(value: Any) -> Optional[Date]:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return None
            try:
                return pd.to_datetime(value).date()
            except Exception:
                return None

        _ = date
        target = self._to_jq_code(security)
        ts_code = self._to_ts_code(security)
        memo_key = f"secinfo:{ts_code}"

        def _fetch() -> Dict[str, Any]:
            asset = self._infer_asset_by_code(target) or self._infer_asset(security)

            # 股票：走本地 stock_basic 全表缓存（首次一次网络，之后近似 0 成本）
            if asset == "E" or (asset is None):
                row = self._stock_basic_row(ts_code)
                if row:
                    return {
                        "display_name": row.get("name") or target,
                        "name": row.get("name") or target.split(".", 1)[0],
                        "start_date": _normalize_date(row.get("list_date")),
                        "end_date": _normalize_date(row.get("delist_date")) or Date(2200, 1, 1),
                        "type": "stock",
                        "subtype": None,
                        "parent": None,
                        "industry": row.get("industry"),
                    }

            pro = self._ensure_client()
            if asset == "FD" or str(security).upper().endswith(".OF"):
                fund_code = ts_code
                if str(security).upper().endswith(".OF") and not fund_code.upper().endswith(".OF"):
                    fund_code = f"{fund_code.split('.')[0]}.OF"
                for kwargs in ({"ts_code": fund_code, "market": "O"}, {"ts_code": fund_code}, {"ts_code": fund_code, "market": "E"}):
                    try:
                        df = pro.fund_basic(**kwargs)
                    except Exception:
                        continue
                    if df is not None and not df.empty:
                        r = df.iloc[0]
                        return {
                            "display_name": r.get("name") or target,
                            "name": r.get("name") or target.split(".", 1)[0],
                            "start_date": _normalize_date(r.get("found_date") or r.get("list_date")),
                            "end_date": _normalize_date(r.get("delist_date")) or Date(2200, 1, 1),
                            "type": "fund",
                            "subtype": None,
                            "parent": None,
                        }

            if asset == "I":
                try:
                    df = pro.index_basic(ts_code=ts_code)
                    if df is not None and not df.empty:
                        r = df.iloc[0]
                        return {
                            "display_name": r.get("name") or target,
                            "name": r.get("name") or target.split(".", 1)[0],
                            "start_date": _normalize_date(r.get("list_date") or r.get("base_date")),
                            "end_date": Date(2200, 1, 1),
                            "type": "index",
                            "subtype": None,
                            "parent": None,
                        }
                except Exception:
                    pass

            return {
                "display_name": target,
                "name": target.split(".", 1)[0],
                "start_date": None,
                "end_date": Date(2200, 1, 1),
                "type": "stock",
                "subtype": None,
                "parent": None,
            }

        return self._memo_call(memo_key, _fetch)

    def get_trade_day(self, security: Union[str, List[str]], query_dt: Union[str, datetime]) -> Any:
        try:
            trade_days = self.get_trade_days(end_date=query_dt, count=1)
        except Exception:
            trade_days = []
        if not trade_days:
            last_day = None
        else:
            last_value = trade_days[-1]
            try:
                last_day = pd.to_datetime(last_value).date()
            except Exception:
                last_day = last_value
        if isinstance(security, (list, tuple, set)):
            securities = list(security)
        else:
            securities = [security]
        return {str(sec): last_day for sec in securities}

    # ------------------------ Live 快照 ------------------------
    def get_live_current(self, security: str) -> Dict[str, Any]:
        """
        返回实盘当前快照（最小字段）基于 tushare：
        - last_price: 当前价（优先 realtime / current_tick，再回退日线）
        - high_limit/low_limit: 当日涨跌停价（若可获取）
        - paused: 默认 False
        """
        try:
            last_price = None
            tick = self.get_current_tick(security)
            if isinstance(tick, dict) and tick.get("last_price") is not None:
                last_price = float(tick["last_price"])
            df = self.get_price(security, count=1, frequency="daily", fq="none")
            if df is None or df.empty:
                if last_price is None:
                    return {}
                return {"last_price": last_price, "high_limit": 0.0, "low_limit": 0.0, "paused": False}
            row = df.iloc[-1]
            if last_price is None:
                last_price = float(row.get("close") or 0.0)
            return {
                "last_price": last_price,
                "high_limit": float(row.get("up_limit") or row.get("high_limit") or 0.0),
                "low_limit": float(row.get("down_limit") or row.get("low_limit") or 0.0),
                "paused": False,
            }
        except Exception:
            return {}

    # ------------------------ 分红 / 拆分 ------------------------
    def get_split_dividend(
        self,
        security: str,
        start_date: Optional[Union[str, datetime, Date]] = None,
        end_date: Optional[Union[str, datetime, Date]] = None,
    ) -> List[Dict[str, Any]]:
        kwargs = {
            "security": security,
            "start_date": self._format_date(start_date),
            "end_date": self._format_date(end_date),
        }

        def _fetch(kw: Dict[str, Any]) -> List[Dict[str, Any]]:
            def _parse_date(value: Optional[str]) -> Optional[Date]:
                if not value:
                    return None
                try:
                    return pd.to_datetime(value).date()
                except Exception:
                    return None

            def _safe_float(value: Any) -> Optional[float]:
                try:
                    if value is None or (isinstance(value, float) and pd.isna(value)):
                        return None
                    val = float(value)
                    if pd.isna(val):
                        return None
                    return val
                except (TypeError, ValueError):
                    return None

            pro = self._ensure_client()
            sec = kw["security"]
            ts_code = self._to_ts_code(sec)
            sec_jq = self._to_jq_code(sec)
            start_dt = _parse_date(kw.get("start_date"))
            end_dt = _parse_date(kw.get("end_date"))

            def _in_range(check: Optional[Date]) -> bool:
                if check is None:
                    return False
                if start_dt and check < start_dt:
                    return False
                if end_dt and check > end_dt:
                    return False
                return True

            # 判断证券类型：基金/ETF代码通常以5开头（如511880），股票为6位数字
            code_only = sec_jq.split(".")[0] if "." in sec_jq else sec_jq
            is_fund = code_only.startswith("5") and len(code_only) == 6

            events: List[Dict[str, Any]] = []

            if is_fund:
                # 基金分红：使用 fund_div 接口
                try:
                    seen_dividends = set()
                    df = pro.fund_div(ts_code=ts_code)
                    if df is not None and not df.empty:
                        for _, row in df.iterrows():
                            div_proc = str(row.get("div_proc") or "")
                            if div_proc and "实施" not in div_proc:
                                continue
                            ex_date = _parse_date(row.get("ex_date") or row.get("ann_date"))
                            if not _in_range(ex_date):
                                continue
                            # Tushare 基金分红字段：div_cash 为每份派息
                            cash = _safe_float(row.get("div_cash")) or 0.0
                            signature = (ex_date, round(cash, 6))
                            if signature in seen_dividends:
                                continue
                            seen_dividends.add(signature)
                            events.append(
                                {
                                    "security": sec_jq,
                                    "date": ex_date,
                                    "security_type": "fund",
                                    "scale_factor": 1.0,
                                    "bonus_pre_tax": cash,
                                    "per_base": 1,
                                }
                            )
                except Exception:
                    # 如果基金接口失败，尝试用股票接口
                    pass

            # 股票分红：使用 dividend 接口
            if not is_fund or not events:
                try:
                    df = pro.dividend(ts_code=ts_code)
                    if df is not None and not df.empty:
                        for _, row in df.iterrows():
                            div_proc = str(row.get("div_proc") or "")
                            if div_proc and "实施" not in div_proc:
                                continue
                            ex_date = _parse_date(row.get("ex_date"))
                            if not _in_range(ex_date):
                                continue
                            # Tushare 股票分红字段为每股口径，需转换为每10股
                            cash_pre = _safe_float(row.get("cash_div_tax"))
                            if cash_pre is None or cash_pre == 0.0:
                                cash_pre = _safe_float(row.get("cash_div")) or 0.0
                            stock_paid = _safe_float(row.get("stk_bo_rate")) or 0.0
                            transfer = _safe_float(row.get("stk_co_rate")) or 0.0
                            if stock_paid == 0.0 and transfer == 0.0:
                                stock_paid = _safe_float(row.get("stk_div")) or 0.0
                            per_base = 10
                            scale = 1.0 + stock_paid + transfer
                            events.append(
                                {
                                    "security": sec_jq,
                                    "date": ex_date,
                                    "security_type": "stock",
                                    "scale_factor": scale,
                                    "bonus_pre_tax": cash_pre * per_base,
                                    "per_base": per_base,
                                }
                            )
                except Exception:
                    pass

            return events

        return self._cache.cached_call("get_split_dividend", kwargs, _fetch, result_type="list_dict")


    @staticmethod
    def _unit_to_frequency(unit: str) -> str:
        u = (unit or "1d").lower()
        mapping = {
            "1d": "daily",
            "d": "daily",
            "daily": "daily",
            "1m": "1min",
            "1min": "1min",
            "5m": "5min",
            "5min": "5min",
            "15m": "15min",
            "15min": "15min",
            "30m": "30min",
            "30min": "30min",
            "60m": "60min",
            "60min": "60min",
        }
        return mapping.get(u, u)

    def get_bars(
        self,
        security: Union[str, List[str]],
        count: int,
        unit: str = "1d",
        fields: Optional[List[str]] = None,
        include_now: bool = False,
        end_dt: Optional[Union[str, datetime]] = None,
        fq_ref_date: Union[int, datetime] = 1,
        df: bool = False,
    ) -> Any:
        _ = include_now
        frequency = self._unit_to_frequency(unit)
        pre_factor_ref_date = None if isinstance(fq_ref_date, int) else fq_ref_date
        result = self.get_price(
            security,
            end_date=end_dt,
            frequency=frequency,
            fields=fields,
            fq="pre",
            count=count,
            panel=False if isinstance(security, (list, tuple)) else True,
            pre_factor_ref_date=pre_factor_ref_date,
        )
        if df or isinstance(result, pd.DataFrame):
            return result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
        return result

    def get_ticks(
        self,
        security: str,
        end_dt: Union[str, datetime],
        start_dt: Optional[Union[str, datetime]] = None,
        count: Optional[int] = None,
        fields: Optional[List[str]] = None,
        skip: bool = False,
        df: bool = False,
    ) -> Any:
        """
        历史 tick：优先 ts.get_tick_data(code, date=YYYY-MM-DD, src='tt')。
        无原生 count，先按时间窗口过滤再 tail(count)。
        """
        _ = skip
        ts = self._ensure_ts_module()
        code = self._to_ts_code(security).split(".")[0]
        end_ts = pd.to_datetime(end_dt)
        date_str = end_ts.strftime("%Y-%m-%d")
        # 若 end_dt 带时刻则用其作为上界，否则默认到 15:00:00
        if isinstance(end_dt, datetime) and (end_dt.hour or end_dt.minute or end_dt.second):
            time_hi = end_dt.strftime("%H:%M:%S")
        elif end_ts.hour or end_ts.minute or end_ts.second:
            time_hi = end_ts.strftime("%H:%M:%S")
        else:
            time_hi = "15:00:00"
        time_lo = None
        if start_dt is not None:
            start_ts = pd.to_datetime(start_dt)
            if start_ts.normalize() == end_ts.normalize():
                time_lo = start_ts.strftime("%H:%M:%S")

        # 注意：get_tick_data 走旧版行情源（tt/nt/sn），不经过 TUSHARE_CUSTOM_URL
        preferred = str(self._tick_src or "tt")
        src_order = [preferred] + [s for s in ("tt", "nt", "sn") if s != preferred]
        raw = None
        last_err: Optional[Exception] = None
        used_src = preferred
        for src in src_order:
            try:
                cand = ts.get_tick_data(code, date=date_str, src=src)
            except Exception as exc:
                last_err = exc
                continue
            if cand is None or (isinstance(cand, pd.DataFrame) and cand.empty):
                continue
            raw = cand
            used_src = src
            break
        if raw is None:
            raise RuntimeError(
                f"get_tick_data 无数据 code={code} date={date_str} src={src_order}"
                + (f" ({last_err})" if last_err else "；该接口不走自定义 pro 代理，依赖 tt/nt/sn 外网源")
            )
        _ = used_src
        out = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        if "time" in out.columns:
            times = out["time"].astype(str)
            if time_lo:
                out = out[times >= time_lo]
                times = out["time"].astype(str)
            out = out[times <= time_hi]
        if fields:
            keep = [c for c in fields if c in out.columns]
            if keep:
                out = out[keep]
        if count:
            out = out.tail(int(count))
        out = out.reset_index(drop=True)
        return out if df else out.to_dict(orient="records")

    def get_current_tick(
        self,
        security: str,
        dt: Optional[Union[str, datetime]] = None,
        df: bool = False,
    ) -> Optional[Any]:
        _ = dt
        ts = self._ensure_ts_module()
        ts_code = self._to_ts_code(security)
        code = ts_code.split(".")[0]
        tick: Optional[Dict[str, Any]] = None

        # 1) 旧版实时行情
        try:
            quotes = ts.get_realtime_quotes(code)
            if quotes is not None and not quotes.empty:
                row = quotes.iloc[0]
                tick = {
                    "code": security,
                    "last_price": float(row.get("price") or 0.0),
                    "volume": float(row.get("volume") or 0.0),
                    "money": float(row.get("amount") or 0.0),
                    "time": row.get("time"),
                    "date": row.get("date"),
                }
        except Exception:
            tick = None

        # 2) 回退最近日线收盘
        if tick is None:
            try:
                hist = self.get_price(security, count=1, frequency="daily", fq="none")
                if hist is not None and not hist.empty:
                    row = hist.iloc[-1]
                    tick = {
                        "code": security,
                        "last_price": float(row.get("close") or 0.0),
                        "volume": float(row.get("volume") or 0.0),
                        "money": float(row.get("money") or 0.0),
                        "time": None,
                        "date": str(hist.index[-1].date()) if hasattr(hist.index[-1], "date") else None,
                    }
            except Exception:
                return None if not df else pd.DataFrame()

        if tick is None:
            return None if not df else pd.DataFrame()
        return pd.DataFrame([tick]) if df else tick

    def get_extras(
        self,
        info: str,
        security_list: List[str],
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        df: bool = True,
        count: Optional[int] = None,
    ) -> Any:
        info_key = (info or "").lower()
        securities = list(security_list or [])
        days = self.get_trade_days(start_date=start_date, end_date=end_date, count=count)
        if not days:
            return pd.DataFrame() if df else {}

        if info_key == "is_st":
            name_map: Dict[str, str] = {}
            for sec in securities:
                try:
                    meta = self.get_security_info(sec)
                    name_map[sec] = str(meta.get("display_name") or meta.get("name") or "")
                except Exception:
                    name_map[sec] = ""
            data = {
                sec: [("ST" in name_map.get(sec, "").upper()) for _ in days]
                for sec in securities
            }
            frame = pd.DataFrame(data, index=pd.to_datetime(days))
            return frame if df else frame.to_dict(orient="list")

        raise NotImplementedError(f"Tushare get_extras 暂不支持 info={info}")

    def _daily_basic_valuation(self, trade_date: str) -> pd.DataFrame:
        memo_key = f"daily_basic:{trade_date}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> pd.DataFrame:
            pro = self._ensure_client()
            # 精简字段，降低代理传输
            df = pro.daily_basic(
                trade_date=trade_date,
                fields="ts_code,trade_date,total_mv,circ_mv,pe,pb",
            )
            if df is None or df.empty:
                return pd.DataFrame(
                    columns=["code", "market_cap", "circulating_market_cap", "pe_ratio", "pb_ratio", "day"]
                )
            # 向量化后缀替换，避免逐行 Python map
            codes = df["ts_code"].astype(str).str.replace(".SH", ".XSHG", regex=False)
            codes = codes.str.replace(".SZ", ".XSHE", regex=False)
            return pd.DataFrame(
                {
                    "code": codes,
                    "market_cap": pd.to_numeric(df["total_mv"], errors="coerce") / 10000.0,
                    "circulating_market_cap": pd.to_numeric(df["circ_mv"], errors="coerce") / 10000.0,
                    "pe_ratio": pd.to_numeric(df.get("pe"), errors="coerce"),
                    "pb_ratio": pd.to_numeric(df.get("pb"), errors="coerce"),
                    "day": pd.to_datetime(df["trade_date"], errors="coerce"),
                }
            )

        out = self._cache.cached_call(
            "daily_basic_valuation",
            {"trade_date": trade_date},
            _fetch,
            result_type="df",
        )
        return self._memo_set(memo_key, out)

    def get_fundamentals(
        self,
        query_object: Any,
        date: Optional[Union[str, datetime]] = None,
        statDate: Optional[str] = None,
    ) -> Any:
        """
        对齐 jq get_fundamentals 的常用估值字段（market_cap 等）。
        query_object 可为 None / dict / jq query（过滤条件尽力忽略，由调用方再筛）。
        """
        _ = query_object, statDate
        trade_date = self._format_date(date)
        if not trade_date:
            last = self._latest_trade_day()
            trade_date = self._format_date(last) or datetime.today().strftime("%Y%m%d")
        return self._daily_basic_valuation(trade_date)

    def get_fundamentals_continuously(
        self,
        query_object: Any,
        end_date: Optional[Union[str, datetime]] = None,
        count: int = 1,
        panel: bool = True,
    ) -> Any:
        _ = query_object, panel
        days = self.get_trade_days(end_date=end_date, count=max(int(count or 1), 1))
        trade_dates = [self._format_date(day) for day in days]
        trade_dates = [d for d in trade_dates if d]
        if not trade_dates:
            return pd.DataFrame()

        # 先吃 mem/disk 命中，仅对未命中日期打网；冷启动并行
        frames: List[pd.DataFrame] = []
        missing: List[str] = []
        for d in trade_dates:
            hit, cached = self._memo_get(f"daily_basic:{d}")
            if hit and cached is not None and not getattr(cached, "empty", False):
                frames.append(cached)
            else:
                missing.append(d)

        if missing:
            max_workers = min(4, len(missing))
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futs = {pool.submit(self._daily_basic_valuation, d): d for d in missing}
                    for fut in as_completed(futs):
                        part = fut.result()
                        if part is not None and not part.empty:
                            frames.append(part)
            except Exception:
                for d in missing:
                    part = self._daily_basic_valuation(d)
                    if part is not None and not part.empty:
                        frames.append(part)

        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        if "day" in out.columns:
            out = out.sort_values("day")
        return out

    def _sw_member_table(self) -> pd.DataFrame:
        """
        申万成分全表（代理上 index_member 不存在，index_member_all 常忽略 index_code）。
        全表缓存后本地按 l1/l2/l3_code 过滤。
        """
        memo_key = "sw_member_all_table"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> pd.DataFrame:
            pro = self._ensure_client()
            try:
                df = pro.index_member_all()
            except Exception:
                try:
                    df = pro.index_member_all(index_code="801010.SI")
                except Exception:
                    return pd.DataFrame()
            return df if df is not None else pd.DataFrame()

        df = self._cache.cached_call("sw_member_all", {}, _fetch, result_type="df")
        return self._memo_set(memo_key, df)

    def get_industry_stocks(self, industry_code: str, date: Optional[Union[str, datetime]] = None) -> List[str]:
        """行业成分股：申万代码（如 801010.SI）本地过滤当前成分。"""
        _ = date
        code = str(industry_code or "").strip()
        if code.upper().startswith("HY"):
            def _sw_l1_first() -> str:
                def _fetch_classify() -> str:
                    try:
                        pro = self._ensure_client()
                        classify = pro.index_classify(level="L1", src="SW2021")
                        if classify is not None and not classify.empty:
                            return str(classify.iloc[0].get("index_code") or "")
                    except Exception:
                        return ""
                    return ""

                return self._memo_call("sw_l1_first", _fetch_classify)

            code = _sw_l1_first()
        if not code:
            return []

        memo_key = f"industry_stocks:{code}"

        def _fetch() -> List[str]:
            df = self._sw_member_table()
            if df is None or df.empty:
                return []
            mask = pd.Series(False, index=df.index)
            for col in ("l1_code", "l2_code", "l3_code", "index_code"):
                if col in df.columns:
                    mask = mask | (df[col].astype(str) == code)
            df = df.loc[mask]
            if df.empty:
                return []
            if "is_new" in df.columns:
                cur = df[df["is_new"].astype(str).str.upper() == "Y"]
                if not cur.empty:
                    df = cur
            col = "ts_code" if "ts_code" in df.columns else ("con_code" if "con_code" in df.columns else None)
            if not col:
                return []
            codes = df[col].dropna().astype(str)
            codes = codes.str.replace(".SH", ".XSHG", regex=False).str.replace(".SZ", ".XSHE", regex=False)
            return codes.tolist()

        return self._memo_call(memo_key, _fetch)

    def get_industry(self, security: Union[str, List[str]], date: Optional[Union[str, datetime]] = None) -> Any:
        _ = date
        securities = security if isinstance(security, (list, tuple, set)) else [security]
        result: Dict[str, Any] = {}
        for sec in securities:
            ts_code = self._to_ts_code(str(sec))
            industry_info: Dict[str, Any] = {}
            row = self._stock_basic_row(ts_code)
            if row and row.get("industry"):
                industry_info["jq_l1"] = {
                    "industry_code": None,
                    "industry_name": row.get("industry"),
                }
            result[str(sec)] = industry_info
        return result

    @staticmethod
    def _is_unsupported_api_error(exc: Exception) -> bool:
        msg = str(exc)
        return ("接口不存在" in msg) or ("请指定正确的接口名" in msg) or ("该接口" in msg and "不存在" in msg)

    def _concept_api_enabled(self) -> bool:
        hit, val = self._memo_get("concept_api_enabled")
        if hit:
            return bool(val)
        # 未知时先当作可用，真正调用失败后再标记
        return True

    def _mark_concept_api(self, enabled: bool) -> None:
        self._memo_set("concept_api_enabled", bool(enabled))

    def _concept_catalog(self) -> pd.DataFrame:
        """概念列表（轻量），用于校验/回退 concept_code。"""
        if not self._concept_api_enabled():
            return pd.DataFrame()

        def _fetch() -> pd.DataFrame:
            pro = self._ensure_client()
            try:
                df = pro.concept(src="ts")
            except Exception as exc:
                if self._is_unsupported_api_error(exc):
                    self._mark_concept_api(False)
                return pd.DataFrame()
            return df if df is not None else pd.DataFrame()

        return self._memo_call("concept_catalog", _fetch)

    def _normalize_concept_code(self, concept_code: str) -> str:
        code = str(concept_code or "").strip()
        if not code or code.upper() in {"TS0", "NONE", "NULL"}:
            cat = self._concept_catalog()
            if cat is not None and not cat.empty:
                for col in ("code", "id", "concept_code"):
                    if col in cat.columns and pd.notna(cat.iloc[0].get(col)):
                        return str(cat.iloc[0][col])
            return ""
        return code

    def get_concept_stocks(self, concept_code: str, date: Optional[Union[str, datetime]] = None) -> List[str]:
        _ = date
        if not self._concept_api_enabled():
            return []
        code = self._normalize_concept_code(concept_code)
        if not code:
            return []
        memo_key = f"concept_stocks:{code}"

        def _fetch() -> List[str]:
            pro = self._ensure_client()
            try:
                df = pro.concept_detail(id=code)
            except Exception as exc:
                if self._is_unsupported_api_error(exc):
                    self._mark_concept_api(False)
                return []
            if df is None or df.empty or "ts_code" not in df.columns:
                return []
            return [self._to_jq_code(c) for c in df["ts_code"].dropna().tolist()]

        return self._memo_call(memo_key, _fetch)

    def get_concept(self, security: Union[str, List[str]], date: Optional[Union[str, datetime]] = None) -> Any:
        _ = date
        securities = list(security if isinstance(security, (list, tuple, set)) else [security])
        empty = {str(sec): {"jq_concept": []} for sec in securities}
        if not self._concept_api_enabled():
            return empty

        result: Dict[str, Any] = {}
        pro = self._ensure_client()
        for sec in securities:
            ts_code = self._to_ts_code(str(sec))
            memo_key = f"concept:{ts_code}"

            def _fetch(code: str = ts_code) -> List[Dict[str, Any]]:
                try:
                    try:
                        df = pro.concept_detail(
                            ts_code=code,
                            fields="id,concept_name,ts_code,name",
                        )
                    except TypeError:
                        df = pro.concept_detail(ts_code=code)
                except Exception as exc:
                    if self._is_unsupported_api_error(exc):
                        self._mark_concept_api(False)
                    return []
                if df is None or df.empty:
                    return []
                id_col = "id" if "id" in df.columns else ("concept_code" if "concept_code" in df.columns else None)
                name_col = "concept_name" if "concept_name" in df.columns else ("name" if "name" in df.columns else None)
                codes = df[id_col].astype(str).tolist() if id_col else [None] * len(df)
                names = df[name_col].astype(str).tolist() if name_col else [None] * len(df)
                return [{"concept_code": c, "concept_name": n} for c, n in zip(codes, names)]

            concepts = self._memo_call(memo_key, _fetch)
            result[str(sec)] = {"jq_concept": concepts}
        return result

    def get_fund_info(self, security: str, date: Optional[Union[str, datetime]] = None) -> Any:
        _ = date
        ts_code = self._to_ts_code(security)
        if str(security).upper().endswith(".OF") and not ts_code.upper().endswith(".OF"):
            ts_code = f"{security.split('.')[0]}.OF"
        elif not ts_code.upper().endswith((".OF", ".SH", ".SZ")):
            # 裸代码默认按场外基金试
            ts_code = f"{ts_code}.OF"
        memo_key = f"fund_info:{ts_code}"

        def _fetch() -> Dict[str, Any]:
            pro = self._ensure_client()
            df = None
            # 场外优先 market='O'，避免无效代码空转
            trials = []
            if ts_code.upper().endswith(".OF"):
                trials = [{"ts_code": ts_code, "market": "O"}, {"ts_code": ts_code}]
            else:
                trials = [{"ts_code": ts_code, "market": "E"}, {"ts_code": ts_code}]
            try:
                for kwargs in trials:
                    df = pro.fund_basic(**kwargs)
                    if df is not None and not df.empty:
                        break
            except Exception as exc:
                raise RuntimeError(f"get_fund_info 失败: {exc}") from exc
            if df is None or df.empty:
                return {}
            row = df.iloc[0].to_dict()
            return {
                "fund_name": row.get("name"),
                "fund_type": row.get("fund_type"),
                "start_date": row.get("found_date") or row.get("list_date"),
                "end_date": row.get("delist_date"),
                "advisor": row.get("management"),
                "trustee": row.get("custodian"),
                "raw": row,
            }

        return self._memo_call(memo_key, _fetch)

    def _margin_stocks(self, date: Optional[Union[str, datetime]] = None) -> List[str]:
        trade_date = self._format_date(date)
        if not trade_date:
            last = self._latest_trade_day()
            trade_date = self._format_date(last) or datetime.today().strftime("%Y%m%d")
        memo_key = f"margin_secs:{trade_date}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> List[str]:
            pro = self._ensure_client()
            api = getattr(pro, "margin_secs", None)
            if api is None:
                return []
            try:
                df = api(trade_date=trade_date)
            except Exception:
                return []
            if df is None or df.empty or "ts_code" not in df.columns:
                return []
            codes = df["ts_code"].dropna().astype(str)
            codes = codes.str.replace(".SH", ".XSHG", regex=False).str.replace(".SZ", ".XSHE", regex=False)
            return sorted(codes.unique().tolist())

        out = self._cache.cached_call(
            "margin_secs",
            {"trade_date": trade_date},
            _fetch,
            result_type="list_str",
        )
        return self._memo_set(memo_key, out)

    def get_margincash_stocks(self, date: Optional[Union[str, datetime]] = None) -> Any:
        return self._margin_stocks(date)

    def get_marginsec_stocks(self, date: Optional[Union[str, datetime]] = None) -> Any:
        # 与融资标的同源接口，直接复用记忆缓存
        return self._margin_stocks(date)

    def get_dominant_future(self, underlying_symbol: str, date: Optional[Union[str, datetime]] = None) -> Any:
        pro = self._ensure_client()
        trade_date = self._format_date(date) or datetime.today().strftime("%Y%m%d")
        symbol = str(underlying_symbol or "").upper()
        # IF -> IF.CFX 等
        ts_code = symbol if "." in symbol else f"{symbol}.CFX"
        try:
            if hasattr(pro, "fut_mapping"):
                df = pro.fut_mapping(ts_code=ts_code, trade_date=trade_date)
                if df is not None and not df.empty:
                    col = "mapping_ts_code" if "mapping_ts_code" in df.columns else "ts_code"
                    return str(df.iloc[0][col])
            if hasattr(pro, "fut_dominant"):
                df = pro.fut_dominant(original=symbol, trade_date=trade_date)
                if df is not None and not df.empty:
                    return str(df.iloc[0].get("ts_code") or df.iloc[0].get("dominant"))
        except Exception as exc:
            raise RuntimeError(f"get_dominant_future 失败: {exc}") from exc
        return None

    def _fut_basic_table(self, exchange: str) -> pd.DataFrame:
        memo_key = f"fut_basic:{exchange or 'ALL'}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> pd.DataFrame:
            pro = self._ensure_client()
            try:
                df = pro.fut_basic(exchange=exchange, fut_type="1") if exchange else pro.fut_basic(fut_type="1")
            except Exception:
                return pd.DataFrame()
            return df if df is not None else pd.DataFrame()

        df = self._cache.cached_call(
            "fut_basic",
            {"exchange": exchange or "", "fut_type": "1"},
            _fetch,
            result_type="df",
        )
        return self._memo_set(memo_key, df)

    def get_future_contracts(self, underlying_symbol: str, date: Optional[Union[str, datetime]] = None) -> Any:
        symbol = str(underlying_symbol or "").upper()
        exchange = "CFFEX" if symbol in {"IF", "IH", "IC", "IM", "T", "TF", "TS"} else ""
        date_str = self._format_date(date)
        memo_key = f"fut_contracts:{exchange}:{symbol}:{date_str}"

        def _fetch() -> List[str]:
            df = self._fut_basic_table(exchange)
            if df is None or df.empty:
                return []
            out = df
            if symbol:
                if "fut_code" in out.columns:
                    out = out[out["fut_code"].astype(str).str.upper() == symbol]
                else:
                    out = out[out["ts_code"].astype(str).str.upper().str.startswith(symbol)]
            if date_str and not out.empty:
                # 仅保留在查询日仍挂牌的合约
                if "list_date" in out.columns:
                    listed = out["list_date"].astype(str).fillna("")
                    out = out[(listed == "") | (listed <= date_str)]
                if "delist_date" in out.columns:
                    delist = out["delist_date"].astype(str).fillna("")
                    out = out[(delist == "") | (delist == "None") | (delist >= date_str)]
            if out.empty:
                return []
            return [str(c) for c in out["ts_code"].dropna().tolist()]

        return self._memo_call(memo_key, _fetch)

    def get_billboard_list(
        self,
        stock_list: Optional[List[str]] = None,
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        count: Optional[int] = None,
    ) -> Any:
        """
        龙虎榜。该代理 top_list 必填 trade_date；带 ts_code 时可能一次返回该股历史上榜。
        策略：单次 trade_date+ts_code，本地按日期过滤，并做 mem/disk 缓存。
        """
        pro = self._ensure_client()
        start_str = self._format_date(start_date)
        end_str = self._format_date(end_date)
        if count and not start_str and end_str:
            days = self.get_trade_days(end_date=end_date, count=count)
            if days:
                start_str = self._format_date(days[0])
        if not end_str:
            end_str = datetime.today().strftime("%Y%m%d")

        def _filter_dates(df: pd.DataFrame) -> pd.DataFrame:
            if df is None or df.empty or "trade_date" not in df.columns:
                return df if df is not None else pd.DataFrame()
            td = df["trade_date"].astype(str)
            if start_str:
                df = df[td >= start_str]
                td = df["trade_date"].astype(str)
            if end_str:
                df = df[td <= end_str]
            return df

        if stock_list:
            frames = []
            for sec in stock_list:
                ts_code = self._to_ts_code(sec)
                memo_key = f"top_list:{ts_code}:{start_str}:{end_str}"
                hit, cached = self._memo_get(memo_key)
                if hit:
                    if cached is not None and not getattr(cached, "empty", True):
                        frames.append(cached)
                    continue

                def _fetch(_kw: Dict[str, Any], code: str = ts_code) -> pd.DataFrame:
                    # 1) 单次：trade_date + ts_code（代理上 ~1s，可能含历史）
                    try:
                        part = pro.top_list(trade_date=end_str, ts_code=code)
                    except Exception:
                        part = None
                    if part is None or part.empty:
                        # 2) 回退：最多 3 个交易日，避免 5×RTT
                        days = self.get_trade_days(start_date=start_str, end_date=end_str)
                        if len(days) > 3:
                            days = days[-3:]
                        chunks = []
                        for day in days:
                            trade_date = self._format_date(day)
                            try:
                                chunk = pro.top_list(trade_date=trade_date, ts_code=code)
                            except Exception:
                                continue
                            if chunk is not None and not chunk.empty:
                                chunks.append(chunk)
                        part = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
                    part = _filter_dates(part)
                    if part is not None and not part.empty and "ts_code" in part.columns:
                        part = part.copy()
                        part["code"] = part["ts_code"].map(self._to_jq_code)
                    return part if part is not None else pd.DataFrame()

                part = self._cache.cached_call(
                    "top_list_stock",
                    {"ts_code": ts_code, "start_date": start_str, "end_date": end_str},
                    _fetch,
                    result_type="df",
                )
                self._memo_set(memo_key, part)
                if part is not None and not part.empty:
                    frames.append(part)
            if not frames:
                return pd.DataFrame()
            return pd.concat(frames, ignore_index=True)

        # 全市场：只取 end 日前最多 3 日
        days = self.get_trade_days(start_date=start_str, end_date=end_str)
        if len(days) > 3:
            days = days[-3:]
        frames = []
        for day in days:
            trade_date = self._format_date(day)
            memo_key = f"top_list:ALL:{trade_date}"

            def _fetch_day(_kw: Dict[str, Any], td: str = trade_date) -> pd.DataFrame:
                try:
                    return pro.top_list(trade_date=td)
                except Exception:
                    return pd.DataFrame()

            part = self._memo_call(
                memo_key,
                lambda: self._cache.cached_call(
                    "top_list_day",
                    {"trade_date": trade_date},
                    _fetch_day,
                    result_type="df",
                ),
            )
            if part is not None and not part.empty:
                frames.append(part)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        df = _filter_dates(df)
        if "ts_code" in df.columns:
            df = df.copy()
            df["code"] = df["ts_code"].map(self._to_jq_code)
        return df

    def get_locked_shares(
        self,
        stock_list: List[str],
        start_date: Optional[Union[str, datetime]] = None,
        end_date: Optional[Union[str, datetime]] = None,
        forward_count: Optional[int] = None,
    ) -> Any:
        _ = forward_count
        start_str = self._format_date(start_date)
        end_str = self._format_date(end_date)
        frames = []
        pro = self._ensure_client()
        for sec in stock_list or []:
            ts_code = self._to_ts_code(sec)
            memo_key = f"share_float:{ts_code}:{start_str}:{end_str}"

            def _fetch(code: str = ts_code) -> pd.DataFrame:
                try:
                    part = pro.share_float(ts_code=code, start_date=start_str, end_date=end_str)
                except Exception:
                    return pd.DataFrame()
                if part is None or part.empty:
                    return pd.DataFrame()
                return part

            part = self._memo_call(memo_key, _fetch)
            if part is not None and not part.empty:
                part = part.copy()
                part["code"] = self._to_jq_code(sec)
                frames.append(part)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def subscribe_ticks(self, symbols: List[str]) -> None:
        """
        订阅指定标的 tick（可选实现）。默认不操作。
        """
        return None

    def subscribe_markets(self, markets: List[str]) -> None:
        """
        订阅市场级 tick（可选实现，如 ['SH','SZ']）。默认不操作。
        """
        return None

    def unsubscribe_ticks(self, symbols: Optional[List[str]] = None) -> None:
        """
        取消 tick 订阅（可选实现）。symbols 为 None 表示全部取消。默认不操作。
        """
        return None

    def unsubscribe_markets(self, markets: Optional[List[str]] = None) -> None:
        """
        取消市场级 tick 订阅（可选实现）。默认不操作。
        """
        return None
