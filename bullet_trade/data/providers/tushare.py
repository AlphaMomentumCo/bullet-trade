from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as Date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd

from .base import DataProvider
from .tushare_clickhouse import TushareClickHouseClient, build_clickhouse_client
from ..cache import CacheManager


class TushareProvider(DataProvider):
    """基于 tushare.pro 的数据提供者，字段与复权口径对齐兼容层约定。

    可选 ClickHouse 热链路（Docker 本地库）：日线 / 复权因子 /
    交易日历 / 基础信息等优先读本地库，未命中再回退远程 API。
    起库与 env 配置见 docs/data/DATA_PROVIDER_TUSHARE.md。
    """

    name: str = "tushare"
    _TS_SUFFIX_TO_JQ = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBEI", "BSE": "XBEI"}
    _JQ_SUFFIX_TO_TS = {"XSHG": "SH", "XSHE": "SZ", "XBEI": "BJ", "BJ": "BJ", "BSE": "BJ"}

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
        self._ch: Optional[TushareClickHouseClient] = build_clickhouse_client(self.config)
        # types=stock 默认不含北交所（与聚宽 get_all_securities 对齐）；设 TUSHARE_INCLUDE_BSE=1 可保留
        include_bse = self.config.get("include_bse")
        if include_bse is None:
            include_bse = os.getenv("TUSHARE_INCLUDE_BSE", "0") in ("1", "true", "True")
        self._include_bse = bool(include_bse)

    def _ch_available(self) -> bool:
        return self._ch is not None and self._ch.is_available()

    # ------------------------ 公共工具 ------------------------
    @classmethod
    def _map_bse_numeric(cls, code: str) -> str:
        """北交所老代码 43/83/87/821 → 920 新码（仅数字段）。"""
        c = str(code or "").split(".", 1)[0].strip()
        if not c.isdigit():
            return c
        if c.startswith("821") and len(c) >= 6:
            return "920" + c[3:]
        if c.startswith(("43", "83", "87")) and len(c) >= 6:
            return "92" + c[2:]
        return c

    @classmethod
    def _is_bse_ts_code(cls, ts_code: str) -> bool:
        s = str(ts_code or "").upper()
        if s.endswith((".BJ", ".BSE", ".XBEI")):
            return True
        num = s.split(".", 1)[0]
        return num.startswith(("43", "82", "83", "87", "92")) and len(num) == 6

    @classmethod
    def _to_ts_code(cls, security: str) -> str:
        if not security or not isinstance(security, str) or "." not in security:
            return security
        code, suffix = security.split(".", 1)
        mapped = cls._JQ_SUFFIX_TO_TS.get(suffix.upper())
        if mapped:
            return f"{cls._map_bse_numeric(code)}.{mapped}"
        return security

    @classmethod
    def _to_jq_code(cls, security: str) -> str:
        if not security or not isinstance(security, str) or "." not in security:
            return security
        code, suffix = security.split(".", 1)
        mapped = cls._TS_SUFFIX_TO_JQ.get(suffix.upper())
        if mapped:
            return f"{cls._map_bse_numeric(code)}.{mapped}"
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
        """取行情。单标的 / 多标的同一入口，按数量自动路由：

        - 日线 + ClickHouse 可用：走 ``_get_price_batch_via_ch``（1 票与 N 票同一套）
        - 否则：逐票 ``_get_price_single``（仍可命中 CH 单票或远程）

        ``security`` 可为股票或指数（或列表）；指数与股票勿混在同一列表。
        """
        _ = (fill_paused, prefer_engine, force_no_engine)
        securities = list(security if isinstance(security, (list, tuple)) else [security])
        # 自动路由：有 CH 时优先批量热路径；否则走远程/proxy 批量（逗号多码，避免逐票 HTTP）
        if securities and self._ch_available():
            batched = self._get_price_batch_via_ch(
                securities,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                fields=fields,
                skip_paused=skip_paused,
                fq=fq,
                count=count,
                panel=panel,
                pre_factor_ref_date=pre_factor_ref_date,
            )
            if batched is not None:
                return batched
        if securities:
            batched = self._get_price_batch_via_remote(
                securities,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                fields=fields,
                skip_paused=skip_paused,
                fq=fq,
                count=count,
                panel=panel,
                pre_factor_ref_date=pre_factor_ref_date,
            )
            if batched is not None:
                return batched

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

    def _get_price_batch_via_ch(
        self,
        securities: List[str],
        *,
        start_date: Optional[Union[str, datetime]],
        end_date: Optional[Union[str, datetime]],
        frequency: str,
        fields: Optional[List[str]],
        skip_paused: bool,
        fq: Optional[str],
        count: Optional[int],
        panel: bool,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> Optional[pd.DataFrame]:
        """多标的（含单票）日线走 ClickHouse 批量查询；不适用则返回 None 回退逐票。

        由 ``get_price`` 按 security 列表长度自动调用，不对外暴露独立 API。
        """
        if not self._ch_available() or not securities:
            return None
        freq = self._normalize_frequency(frequency)
        if freq != "D":
            return None

        meta: List[Tuple[str, str, str]] = []  # jq_code, ts_code, asset
        for sec in securities:
            asset = self._infer_asset(sec)
            if asset not in ("E", "I"):
                return None
            meta.append((str(sec), self._to_ts_code(sec), asset))

        assets = {a for _, _, a in meta}
        if len(assets) != 1:
            # 股票/指数混查时拆两批也可，这里简化：回退逐票
            return None
        asset = next(iter(assets))
        end_str = self._format_date(end_date)
        start_str = self._format_date(start_date)
        if count and not start_str:
            start_str = self._estimate_start_for_count(end_date or end_str, int(count), frequency)

        assert self._ch is not None
        ts_codes = [ts for _, ts, _ in meta]
        try:
            if asset == "E" and fq in ("pre", "post"):
                raw_daily, raw_adj = self._ch.fetch_daily_and_adj_batch(
                    ts_codes, start_str, end_str
                )
            elif asset == "E":
                raw_daily = self._ch.fetch_daily_batch(ts_codes, start_str, end_str)
                raw_adj = pd.DataFrame()
            else:
                raw_daily = self._ch.fetch_index_daily_batch(ts_codes, start_str, end_str)
                raw_adj = pd.DataFrame()
        except Exception:
            return None

        if raw_daily is None or raw_daily.empty:
            # 批量空：可能库未覆盖，回退逐票（允许部分远程）
            return None

        # 前复权锚点：批量路径优先用库内最大交易日，避免逐票补拉未来区间 adj
        resolved_ref = pre_factor_ref_date
        if resolved_ref is None and fq == "pre" and asset == "E":
            try:
                max_in_db = pd.to_datetime(raw_daily["trade_date"], errors="coerce").max()
            except Exception:
                max_in_db = None
            latest = self._latest_trade_day()
            if max_in_db is not None and not pd.isna(max_in_db):
                if latest is None or pd.to_datetime(latest) > max_in_db:
                    resolved_ref = max_in_db
                else:
                    resolved_ref = latest
            else:
                resolved_ref = latest

        daily_by = {
            str(code): g
            for code, g in raw_daily.groupby("ts_code", sort=False)
        }
        adj_by: Dict[str, pd.DataFrame] = {}
        if raw_adj is not None and not raw_adj.empty and "ts_code" in raw_adj.columns:
            adj_by = {
                str(code): g
                for code, g in raw_adj.groupby("ts_code", sort=False)
            }

        # 批量矢量化整理（避免逐票 copy/join）；缺失标的再回退单票
        present = [(jq, ts, a) for jq, ts, a in meta if ts in daily_by and not daily_by[ts].empty]
        missing = [(jq, ts, a) for jq, ts, a in meta if ts not in daily_by or daily_by[ts].empty]

        frames: Dict[str, pd.DataFrame] = {}
        if present:
            try:
                frames.update(
                    self._finalize_price_batch_frames(
                        present_meta=present,
                        daily_by=daily_by,
                        adj_by=adj_by,
                        frequency=frequency,
                        fields=fields,
                        skip_paused=skip_paused,
                        fq=fq,
                        count=count,
                        pre_factor_ref_date=resolved_ref,
                        asset=asset,
                    )
                )
            except Exception:
                frames = {}
                for jq_code, ts_code, _asset in present:
                    frames[jq_code] = self._finalize_price_frame(
                        security=jq_code,
                        df=daily_by[ts_code],
                        factor_df=adj_by.get(ts_code),
                        already_qfq=False,
                        frequency=frequency,
                        fields=fields,
                        skip_paused=skip_paused,
                        fq=fq,
                        count=count,
                        pre_factor_ref_date=resolved_ref,
                        asset=_asset,
                        start_str=start_str,
                        end_str=end_str,
                    )

        for jq_code, _ts, _asset in missing:
            frames[jq_code] = self._get_price_single(
                jq_code,
                start_date=start_date,
                end_date=end_date,
                frequency=frequency,
                fields=fields,
                skip_paused=skip_paused,
                fq=fq,
                count=count,
                pre_factor_ref_date=resolved_ref,
                asset=_asset,
            )

        if panel:
            # 保持 meta 顺序；单标的时返回普通 DataFrame（与历史 get_price 单票口径一致）
            ordered = {jq: frames[jq] for jq, _, _ in meta if jq in frames}
            if not ordered:
                return pd.DataFrame()
            if len(ordered) == 1:
                return next(iter(ordered.values()))
            return pd.concat(ordered, axis=1)
        long_rows = []
        for jq, _, _ in meta:
            df = frames.get(jq)
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                continue
            tmp = df.copy()
            tmp["code"] = jq
            long_rows.append(tmp)
        return pd.concat(long_rows, axis=0) if long_rows else pd.DataFrame()

    def _get_price_batch_via_remote(
        self,
        securities: List[str],
        *,
        start_date: Optional[Union[str, datetime]],
        end_date: Optional[Union[str, datetime]],
        frequency: str,
        fields: Optional[List[str]],
        skip_paused: bool,
        fq: Optional[str],
        count: Optional[int],
        panel: bool,
        pre_factor_ref_date: Optional[Union[str, datetime]],
    ) -> Optional[pd.DataFrame]:
        """无 CH 时：一次（或分块）远程/proxy 多码查询，避免逐票 HTTP。

        对 local_tushare_proxy：``daily`` + ``fields`` 含 ``adj_factor`` 会走 ``bt_daily_adj_fast``。
        """
        if not securities:
            return None
        freq = self._normalize_frequency(frequency)
        if freq != "D":
            return None

        meta: List[Tuple[str, str, str]] = []
        for sec in securities:
            asset = self._infer_asset(sec)
            if asset not in ("E", "I"):
                return None
            meta.append((str(sec), self._to_ts_code(sec), asset))
        assets = {a for _, _, a in meta}
        if len(assets) != 1:
            return None
        asset = next(iter(assets))
        end_str = self._format_date(end_date)
        start_str = self._format_date(start_date)
        if count and not start_str:
            start_str = self._estimate_start_for_count(end_date or end_str, int(count), frequency)

        try:
            pro = self._ensure_client()
        except Exception:
            return None

        ts_codes = [ts for _, ts, _ in meta]
        chunk_size = 300
        daily_parts: List[pd.DataFrame] = []
        adj_parts: List[pd.DataFrame] = []
        want_adj = asset == "E" and fq in ("pre", "post")
        # 宽表友好字段：proxy 见 adj_factor 即映射 bt_daily_adj_fast
        daily_fields = "ts_code,trade_date,open,high,low,close,vol,amount"
        if want_adj:
            daily_fields += ",adj_factor"

        try:
            for i in range(0, len(ts_codes), chunk_size):
                batch = ts_codes[i : i + chunk_size]
                code_param = ",".join(batch)
                if asset == "E":
                    part = pro.daily(
                        ts_code=code_param,
                        start_date=start_str,
                        end_date=end_str,
                        fields=daily_fields,
                    )
                else:
                    part = pro.index_daily(
                        ts_code=code_param,
                        start_date=start_str,
                        end_date=end_str,
                    )
                if part is not None and not part.empty:
                    daily_parts.append(part)
                    # 若未带出 adj_factor，再批量补因子
                    if want_adj and "adj_factor" not in part.columns:
                        af = pro.adj_factor(
                            ts_code=code_param,
                            start_date=start_str,
                            end_date=end_str,
                            fields="ts_code,trade_date,adj_factor",
                        )
                        if af is not None and not af.empty:
                            adj_parts.append(af)
        except Exception:
            return None

        if not daily_parts:
            return None
        raw_daily = pd.concat(daily_parts, ignore_index=True)
        raw_adj = pd.concat(adj_parts, ignore_index=True) if adj_parts else pd.DataFrame()

        resolved_ref = pre_factor_ref_date
        if resolved_ref is None and fq == "pre" and asset == "E":
            try:
                max_in_db = pd.to_datetime(raw_daily["trade_date"], errors="coerce").max()
            except Exception:
                max_in_db = None
            latest = self._latest_trade_day()
            if max_in_db is not None and not pd.isna(max_in_db):
                if latest is None or pd.to_datetime(latest) > max_in_db:
                    resolved_ref = max_in_db
                else:
                    resolved_ref = latest
            else:
                resolved_ref = latest

        daily_by = {str(code): g for code, g in raw_daily.groupby("ts_code", sort=False)}
        adj_by: Dict[str, pd.DataFrame] = {}
        if raw_adj is not None and not raw_adj.empty and "ts_code" in raw_adj.columns:
            adj_by = {str(code): g for code, g in raw_adj.groupby("ts_code", sort=False)}

        present = [(jq, ts, a) for jq, ts, a in meta if ts in daily_by and not daily_by[ts].empty]
        missing = [(jq, ts, a) for jq, ts, a in meta if ts not in daily_by or daily_by[ts].empty]
        frames: Dict[str, pd.DataFrame] = {}
        if present:
            try:
                frames.update(
                    self._finalize_price_batch_frames(
                        present_meta=present,
                        daily_by=daily_by,
                        adj_by=adj_by,
                        frequency=frequency,
                        fields=fields,
                        skip_paused=skip_paused,
                        fq=fq,
                        count=count,
                        pre_factor_ref_date=resolved_ref,
                        asset=asset,
                    )
                )
            except Exception:
                return None
        for jq_code, _ts, _asset in missing:
            try:
                frames[jq_code] = self._get_price_single(
                    jq_code,
                    start_date=start_date,
                    end_date=end_date,
                    frequency=frequency,
                    fields=fields,
                    skip_paused=skip_paused,
                    fq=fq,
                    count=count,
                    pre_factor_ref_date=pre_factor_ref_date,
                    asset=_asset,
                )
            except Exception:
                frames[jq_code] = pd.DataFrame()

        if not frames:
            return None
        if panel:
            ordered = {jq: frames[jq] for jq, _, _ in meta if jq in frames}
            if not ordered:
                return pd.DataFrame()
            if len(ordered) == 1:
                return next(iter(ordered.values()))
            return pd.concat(ordered, axis=1)
        long_rows = []
        for jq, _, _ in meta:
            df = frames.get(jq)
            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                continue
            tmp = df.copy()
            tmp["code"] = jq
            long_rows.append(tmp)
        return pd.concat(long_rows, axis=0) if long_rows else pd.DataFrame()

    def _finalize_price_batch_frames(
        self,
        *,
        present_meta: List[Tuple[str, str, str]],
        daily_by: Dict[str, pd.DataFrame],
        adj_by: Dict[str, pd.DataFrame],
        frequency: str,
        fields: Optional[List[str]],
        skip_paused: bool,
        fq: Optional[str],
        count: Optional[int],
        pre_factor_ref_date: Optional[Union[str, datetime]],
        asset: str,
    ) -> Dict[str, pd.DataFrame]:
        """多标的日线一次矢量化整理，返回 {jq_code: frame}。

        若 daily 已含 adj_factor（宽表/JOIN 结果），不再二次 merge。
        """
        freq = self._normalize_frequency(frequency)
        ts_to_jq = {ts: jq for jq, ts, _a in present_meta}
        parts = [daily_by[ts] for _jq, ts, _a in present_meta]
        df = pd.concat(parts, ignore_index=True)
        if "ts_code" not in df.columns:
            raise ValueError("batch daily missing ts_code")
        df = df.copy()
        df["_jq"] = df["ts_code"].map(ts_to_jq)
        df.rename(
            columns={
                "vol": "volume",
                "amount": "money",
            },
            inplace=True,
        )
        if "money" not in df.columns:
            df["money"] = 0.0
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = self._normalize_price_units(df, freq, asset)
        if not pd.api.types.is_datetime64_any_dtype(df["trade_date"]):
            df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        if skip_paused and "is_paused" in df.columns:
            df = df[df["is_paused"] == 0]

        need_adj = asset == "E" and fq in ("pre", "post")
        if need_adj:
            if "adj_factor" not in df.columns:
                adj_parts = []
                for _jq, ts_code, _a in present_meta:
                    af = adj_by.get(ts_code)
                    if af is None or af.empty or "adj_factor" not in af.columns:
                        continue
                    ap = af[["trade_date", "adj_factor"]].copy()
                    ap["ts_code"] = ts_code
                    adj_parts.append(ap)
                if adj_parts:
                    adj = pd.concat(adj_parts, ignore_index=True)
                    adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce")
                    df = df.merge(adj, on=["ts_code", "trade_date"], how="left")
                else:
                    df["adj_factor"] = 1.0
            df["adj_factor"] = (
                df.groupby("ts_code", sort=False)["adj_factor"].ffill().bfill().fillna(1.0)
            )
            # 全 1 因子：跳过乘除（本地 CSV 同步常见）
            if not (df["adj_factor"] == 1.0).all():
                resolved_ref = pre_factor_ref_date
                if resolved_ref is None and fq == "pre":
                    resolved_ref = self._latest_trade_day()
                if resolved_ref is None:
                    resolved_ref = df["trade_date"].max()
                ref_dt = pd.to_datetime(resolved_ref).normalize()
                fac = df[["ts_code", "trade_date", "adj_factor"]].dropna()
                prior = fac[fac["trade_date"] <= ref_dt]
                if prior.empty:
                    ref_map = fac.groupby("ts_code", sort=False)["adj_factor"].last()
                else:
                    idx = prior.groupby("ts_code", sort=False)["trade_date"].idxmax()
                    ref_map = prior.loc[idx].set_index("ts_code")["adj_factor"]
                df["_ref"] = df["ts_code"].map(ref_map).astype(float).replace(0, pd.NA)
                if fq == "pre":
                    ratio = df["adj_factor"] / df["_ref"]
                else:
                    ratio = df["_ref"] / df["adj_factor"]
                ratio = ratio.fillna(1.0)
                for col in ("open", "high", "low", "close"):
                    if col in df.columns:
                        df[col] = df[col].astype(float) * ratio
                if "volume" in df.columns:
                    safe = ratio.replace(0, pd.NA)
                    df["volume"] = (df["volume"].astype(float) / safe).fillna(df["volume"])
                df.drop(columns=["_ref"], inplace=True, errors="ignore")
            df.drop(columns=["adj_factor"], inplace=True, errors="ignore")

        # CH 已 ORDER BY ts_code, trade_date；按 _jq 分组保持相对顺序，避免再 sort
        drop_cols = [c for c in ("ts_code", "_jq", "pre_close", "change", "pct_chg") if c in df.columns]
        out: Dict[str, pd.DataFrame] = {}
        for jq_code, g in df.groupby("_jq", sort=False):
            part = g.drop(columns=[c for c in drop_cols if c in g.columns], errors="ignore")
            part = part.set_index("trade_date")
            if count:
                part = part.tail(int(count))
            part = self._apply_fields(part, fields)
            out[str(jq_code)] = part
        return out

    def _finalize_price_frame(
        self,
        *,
        security: str,
        df: pd.DataFrame,
        factor_df: Optional[pd.DataFrame],
        already_qfq: bool,
        frequency: str,
        fields: Optional[List[str]],
        skip_paused: bool,
        fq: Optional[str],
        count: Optional[int],
        pre_factor_ref_date: Optional[Union[str, datetime]],
        asset: str,
        start_str: Optional[str],
        end_str: Optional[str],
    ) -> pd.DataFrame:
        """将 CH/远程原始 OHLCV(+可选 adj) 整理为 get_price 输出帧。"""
        freq = self._normalize_frequency(frequency)
        ts_code = self._to_ts_code(security)
        mem_key = (
            f"price:{ts_code}:{start_str}:{end_str}:{freq}:{fq}:{count}:"
            f"{self._format_date(pre_factor_ref_date)}:{skip_paused}:{tuple(fields or ())}"
        )
        hit, cached = self._memo_get(mem_key)
        if hit:
            return cached.copy() if isinstance(cached, pd.DataFrame) else cached

        if df is None or df.empty:
            return self._memo_set(mem_key, pd.DataFrame())

        # 宽表：adj_factor 已在日线帧上
        if (
            (factor_df is None or factor_df.empty)
            and "adj_factor" in df.columns
        ):
            factor_df = df[["trade_date", "adj_factor"]].copy() if "trade_date" in df.columns else None

        need_local_adj = (
            asset == "E"
            and fq in ("pre", "post")
            and not already_qfq
            and (factor_df is None or factor_df.empty)
        )
        if need_local_adj and self._ch_available():
            try:
                factor_df = self._fetch_adj_factor(
                    security,
                    pd.to_datetime(start_str or end_str),
                    pd.to_datetime(end_str or start_str),
                )
            except Exception:
                factor_df = None

        time_col = "trade_time" if self._is_minute_frequency(freq) and "trade_time" in df.columns else "trade_date"
        if time_col not in df.columns:
            for cand in ("trade_time", "trade_date", "datetime"):
                if cand in df.columns:
                    time_col = cand
                    break
        out = df.sort_values(time_col).copy()
        out.index = pd.to_datetime(out[time_col])
        out.rename(
            columns={
                "vol": "volume",
                "amount": "money",
                "high_limit": "high_limit",
                "low_limit": "low_limit",
            },
            inplace=True,
        )
        if "ts_code" in out.columns:
            out["ts_code"] = out["ts_code"].map(self._to_jq_code)
        out["money"] = out.get("money", 0.0)
        out["volume"] = out.get("volume", 0.0)
        out = self._normalize_price_units(out, freq, asset)

        if skip_paused and "is_paused" in out.columns:
            out = out[out["is_paused"] == 0]

        if asset == "E" and fq in ("pre", "post") and not already_qfq:
            resolved_ref = pre_factor_ref_date
            if resolved_ref is None and fq == "pre":
                latest = self._latest_trade_day()
                if latest is not None:
                    resolved_ref = latest
            if factor_df is not None and not factor_df.empty and "adj_factor" in factor_df.columns:
                factor_df = self._ensure_adj_factor_covers_ref(
                    security, factor_df, out.index.min(), resolved_ref
                )
                out = self._apply_adjustment_with_factor(
                    out, factor_df, fq=fq, pre_factor_ref_date=resolved_ref
                )
            else:
                out = self._apply_adjustment(
                    security=security,
                    df=out,
                    fq=fq,
                    pre_factor_ref_date=resolved_ref,
                )

        if count:
            out = out.tail(count)
        out = self._apply_fields(out, fields)
        return self._memo_set(mem_key, out)

    def _fetch_ohlcv_raw(
        self,
        ts_code: str,
        asset: str,
        start_str: Optional[str],
        end_str: Optional[str],
        freq: str,
        fq: Optional[str],
        pre_factor_ref_date: Optional[Union[str, datetime]],
        prefer_native_qfq: bool = True,
    ) -> Tuple[pd.DataFrame, bool]:
        """
        拉取 OHLCV。返回 (df, already_qfq)。

        日线优先走 ClickHouse 热链路（未复权），上层再本地复权。
        无本地库时：前复权可走单次 pro_bar(adj='qfq')；失败则 daily + adj_factor。
        """
        # ClickHouse 热路：日线 OHLCV 优先本地库，避免远程代理 RTT
        if freq == "D" and self._ch_available():
            assert self._ch is not None
            try:
                ch_df = self._ch.fetch_ohlcv(ts_code, asset, start_str, end_str)
                if ch_df is not None and not ch_df.empty:
                    return ch_df, False
            except Exception:
                pass

        ts = self._ensure_ts_module()
        pro = self._ensure_client()

        use_native_qfq = (
            prefer_native_qfq
            and freq == "D"
            and asset == "E"
            and fq == "pre"
            and pre_factor_ref_date is None
            and not self._ch_available()  # 有本地库时不做远程 qfq
        )
        if use_native_qfq:
            try:
                df = ts.pro_bar(
                    ts_code=ts_code,
                    start_date=start_str,
                    end_date=end_str,
                    freq=freq,
                    adj="qfq",
                    asset=asset,
                    api=pro,
                )
                if df is not None and not df.empty:
                    return df, True
            except Exception:
                pass
            # 交给上层走 daily+adj_factor，避免此处再打一次 daily
            return pd.DataFrame(), False

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

        # 日线前复权：有 ClickHouse 时直接 daily+adj（本地）；否则可先试远程 pro_bar(qfq)。
        prefer_qfq = (
            asset == "E"
            and freq == "D"
            and fq == "pre"
            and pre_factor_ref_date is None
            and not self._ch_available()
        )

        factor_df = None
        already_qfq = False
        df = pd.DataFrame()

        # CH 热路：日线+复权因子一次 JOIN（省一次 HTTP RTT）
        if (
            freq == "D"
            and asset == "E"
            and fq in ("pre", "post")
            and self._ch_available()
        ):
            assert self._ch is not None
            try:
                df, factor_df = self._ch.fetch_daily_and_adj(ts_code, start_str, end_str)
                already_qfq = False
                # 宽表路径：adj_factor 落在日线帧上
                if (
                    (factor_df is None or factor_df.empty)
                    and df is not None
                    and not df.empty
                    and "adj_factor" in df.columns
                ):
                    factor_df = df[["trade_date", "adj_factor"]].copy()
            except Exception:
                df, factor_df = pd.DataFrame(), None

        if df is None or df.empty:
            df, already_qfq = self._fetch_ohlcv_raw(
                ts_code=ts_code,
                asset=asset,
                start_str=start_str,
                end_str=end_str,
                freq=freq,
                fq=fq,
                pre_factor_ref_date=pre_factor_ref_date,
                prefer_native_qfq=prefer_qfq,
            )

        need_local_adj = (
            asset == "E"
            and fq in ("pre", "post")
            and not already_qfq
            and (df is not None and not df.empty)
            and (factor_df is None or factor_df.empty)
        )
        # ClickHouse 命中未复权日线后，补 adj_factor（同样优先本地库）
        if need_local_adj and self._ch_available():
            try:
                factor_df = self._fetch_adj_factor(
                    security,
                    pd.to_datetime(start_str or end_str),
                    pd.to_datetime(end_str or start_str),
                )
            except Exception:
                factor_df = None

        if (df is None or df.empty) and prefer_qfq:
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_px = pool.submit(
                        self._fetch_ohlcv_raw,
                        ts_code,
                        asset,
                        start_str,
                        end_str,
                        freq,
                        "none",
                        None,
                        False,
                    )
                    fut_adj = pool.submit(
                        self._fetch_adj_factor,
                        security,
                        pd.to_datetime(start_str or end_str),
                        pd.to_datetime(end_str or start_str),
                    )
                    df, already_qfq = fut_px.result()
                    factor_df = fut_adj.result()
            except Exception:
                df, already_qfq = self._fetch_ohlcv_raw(
                    ts_code, asset, start_str, end_str, freq, "none", None, False
                )
                factor_df = None

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

        if asset == "E" and fq in ("pre", "post") and not already_qfq:
            # 前复权默认锚到最新交易日（与聚宽一致），而非查询区间末日
            resolved_ref = pre_factor_ref_date
            if resolved_ref is None and fq == "pre":
                latest = self._latest_trade_day()
                if latest is not None:
                    resolved_ref = latest
            if factor_df is not None and not factor_df.empty and "adj_factor" in factor_df.columns:
                factor_df = self._ensure_adj_factor_covers_ref(
                    security, factor_df, df.index.min(), resolved_ref
                )
                df = self._apply_adjustment_with_factor(
                    df, factor_df, fq=fq, pre_factor_ref_date=resolved_ref
                )
            else:
                df = self._apply_adjustment(
                    security=security,
                    df=df,
                    fq=fq,
                    pre_factor_ref_date=resolved_ref,
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
        if not factor_df.index.is_unique:
            factor_df = factor_df[~factor_df.index.duplicated(keep="last")]
        merged = df.copy()
        merged["_factor_date"] = pd.to_datetime(merged.index).normalize()
        merged = merged.join(factor_df["adj_factor"], on="_factor_date", how="left")
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        ref_date = pre_factor_ref_date
        if ref_date is None and fq == "pre":
            # 与聚宽对齐：优先最新交易日；调用方通常已解析，此处兜底用区间末日
            ref_date = end_dt
        if ref_date is None:
            ref_date = end_dt if fq == "pre" else start_dt
        try:
            ref_dt = pd.to_datetime(ref_date).normalize()
        except Exception:
            ref_dt = pd.to_datetime(end_dt if fq == "pre" else start_dt).normalize()
        if ref_dt in factor_df.index:
            ref_factor = factor_df.loc[ref_dt, "adj_factor"]
            if isinstance(ref_factor, pd.Series):
                ref_factor = ref_factor.iloc[-1]
        else:
            # 参考日无因子时：前复权取不超过参考日的最近因子，否则退回序列端点
            prior = factor_df.index[factor_df.index <= ref_dt]
            if len(prior) > 0 and fq == "pre":
                ref_factor = factor_df.loc[prior.max(), "adj_factor"]
            else:
                ref_factor = merged["adj_factor"].iloc[-1] if fq == "pre" else merged["adj_factor"].iloc[0]
            if isinstance(ref_factor, pd.Series):
                ref_factor = ref_factor.iloc[-1]
        if ref_factor is None or (isinstance(ref_factor, float) and pd.isna(ref_factor)):
            return df
        try:
            ref_factor = float(ref_factor)
        except Exception:
            return df
        if ref_factor == 0.0:
            return df
        ratio = (merged["adj_factor"] / ref_factor) if fq == "pre" else (ref_factor / merged["adj_factor"])
        for col in ["open", "high", "low", "close"]:
            if col in merged.columns:
                merged[col] = merged[col] * ratio
        # 成交量按价格比例反权重（与聚宽 fq=pre 一致）；成交额保持不变
        if "volume" in merged.columns:
            safe = ratio.replace(0, pd.NA)
            merged["volume"] = merged["volume"] / safe
            merged["volume"] = merged["volume"].fillna(df["volume"] if "volume" in df.columns else 0.0)
        merged.drop(columns=["adj_factor", "_factor_date"], inplace=True, errors="ignore")
        return merged

    def _ensure_adj_factor_covers_ref(
        self,
        security: str,
        factor_df: pd.DataFrame,
        start_dt: Any,
        ref_date: Optional[Union[str, datetime]],
    ) -> pd.DataFrame:
        """若前复权参考日晚于已有因子区间，补拉到参考日。"""
        if factor_df is None or factor_df.empty or ref_date is None:
            return factor_df
        try:
            ref_dt = pd.to_datetime(ref_date).normalize()
        except Exception:
            return factor_df
        work = factor_df.copy()
        if "trade_date" in work.columns:
            dates = pd.to_datetime(work["trade_date"], errors="coerce")
        else:
            dates = pd.to_datetime(work.index, errors="coerce")
        if dates.isna().all():
            return factor_df
        max_dt = dates.max()
        if pd.isna(max_dt) or max_dt.normalize() >= ref_dt:
            return factor_df
        try:
            extra = self._fetch_adj_factor(security, pd.to_datetime(start_dt), ref_dt)
        except Exception:
            return factor_df
        if extra is None or extra.empty:
            return factor_df
        merged = pd.concat([work, extra], ignore_index=True)
        if "trade_date" in merged.columns:
            merged = merged.drop_duplicates(subset=["trade_date"], keep="last")
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
        ref_date = pre_factor_ref_date
        if ref_date is None and fq == "pre":
            latest = self._latest_trade_day()
            if latest is not None:
                ref_date = latest
        factor_end = end_dt
        if ref_date is not None:
            try:
                factor_end = max(pd.to_datetime(end_dt), pd.to_datetime(ref_date))
            except Exception:
                factor_end = end_dt
        factor_df = self._fetch_adj_factor(security, start_dt, factor_end)
        if factor_df.empty or "adj_factor" not in factor_df.columns:
            fallback = self._build_adjusted_from_events(
                security=security,
                raw_df=df,
                fq=fq,
                pre_factor_ref_date=ref_date,
            )
            return fallback if not fallback.empty else df
        return self._apply_adjustment_with_factor(df, factor_df, fq=fq, pre_factor_ref_date=ref_date)

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
        if "volume" in adj_df.columns:
            safe = factors.replace(0, pd.NA)
            adj_df["volume"] = adj_df["volume"].astype(float) / safe
            adj_df["volume"] = adj_df["volume"].fillna(raw_df["volume"] if "volume" in raw_df.columns else 0.0)

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
        if "volume" in adj_df.columns and scale != 0:
            adj_df["volume"] = adj_df["volume"] / scale
        return adj_df

    def _latest_trade_day(self) -> Optional[datetime]:
        cached = getattr(self, "_latest_trade_day_cache", None)
        if cached is not None or getattr(self, "_latest_trade_day_checked", False):
            return cached
        try:
            days = self.get_trade_days(end_date=Date.today(), count=1)
            if days:
                self._latest_trade_day_cache = pd.to_datetime(days[-1])
            else:
                self._latest_trade_day_cache = None
        except Exception:
            self._latest_trade_day_cache = None
        self._latest_trade_day_checked = True
        return self._latest_trade_day_cache

    def _fetch_adj_factor(self, security: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        kwargs = {
            "security": security,
            "start_date": start_dt.strftime("%Y%m%d"),
            "end_date": end_dt.strftime("%Y%m%d"),
        }

        def _fetch(kw: Dict[str, Any]) -> pd.DataFrame:
            ts_code = self._to_ts_code(kw["security"])
            if self._ch_available():
                assert self._ch is not None
                ch_df = self._ch.fetch_adj_factor(ts_code, kw["start_date"], kw["end_date"])
                if ch_df is not None and not ch_df.empty:
                    return ch_df
            pro = self._ensure_client()
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
            start = self._format_date(kw.get("start_date"))
            end = self._format_date(kw.get("end_date"))
            count = kw.get("count")
            # 仅 count+end 时补 start，避免 trade_cal 拉全历史
            if count and end and not start and count != -1:
                end_dt = pd.to_datetime(end)
                start = (end_dt - pd.Timedelta(days=int(count) * 3 + 20)).strftime("%Y%m%d")

            if self._ch_available():
                assert self._ch is not None
                # 开市日窄表 + 行结果，避免 DF/is_open 过滤
                open_days = self._ch.fetch_open_cal_ymds(start, end, exchange="SSE")
                if open_days:
                    if count and count != -1:
                        open_days = open_days[-int(count) :]
                    return open_days
                ch_df = self._ch.fetch_trade_cal(start, end, exchange="SSE")
                if ch_df is not None and not ch_df.empty and "is_open" in ch_df.columns:
                    open_days = (
                        ch_df[ch_df["is_open"].astype(int) == 1]["cal_date"]
                        .astype(str)
                        .sort_values()
                        .tolist()
                    )
                    if count and count != -1:
                        open_days = open_days[-int(count) :]
                    if open_days:
                        return open_days

            pro = self._ensure_client()
            df = pro.trade_cal(
                exchange="SSE",
                start_date=start,
                end_date=end,
                fields="cal_date,is_open",
            )
            open_days = df[df["is_open"] == 1]["cal_date"].sort_values().tolist()
            if count and count != -1:
                open_days = open_days[-int(count) :]
            return [str(x) for x in open_days]

        memo_key = f"trade_days:{kwargs.get('start_date')}:{kwargs.get('end_date')}:{kwargs.get('count')}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return list(cached)
        date_strs = self._cache.cached_call("get_trade_days", kwargs, _fetch, result_type="list_str")
        # strptime 比逐个 pd.to_datetime 更轻
        out = [datetime.strptime(str(d)[:8], "%Y%m%d") for d in date_strs]
        return self._memo_set(memo_key, out)

    def get_all_securities(
        self,
        types: Union[str, List[str]] = "stock",
        date: Optional[Union[str, datetime]] = None,
    ) -> pd.DataFrame:
        """
        证券列表。

        - types=stock 且传入 date：拉取 list_status=L+D（必要时补 P），再按 list_date/delist_date 过滤。
        - types=stock 且 date 为空：仅 L（当前在市）。
        - 默认剔除北交所（与聚宽对齐）；``include_bse=True`` / ``TUSHARE_INCLUDE_BSE=1`` 可保留。
        """
        if isinstance(types, str):
            types = [types]

        kwargs = {
            "types": tuple(sorted(types)),
            "date": date,
            "include_bse": self._include_bse,
        }
        memo_key = (
            f"all_securities:{kwargs['types']}:{self._format_date(date) or ''}:"
            f"bse={int(self._include_bse)}"
        )
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached.copy() if isinstance(cached, pd.DataFrame) else cached

        def _fetch_stock_basic_frames(need_history: bool) -> pd.DataFrame:
            """need_history=True 时取 L+D(+P)，否则仅 L。"""
            statuses = ["L", "D", "P"] if need_history else ["L"]
            frames: List[pd.DataFrame] = []
            keep_cols = ("ts_code", "name", "list_date", "delist_date", "market", "list_status")

            if self._ch_available():
                assert self._ch is not None
                if need_history:
                    ch_df = self._ch.fetch_stock_basic(list_status=None)
                else:
                    ch_df = self._ch.fetch_stock_basic(list_status="L")
                if ch_df is not None and not ch_df.empty:
                    keep = [c for c in keep_cols if c in ch_df.columns]
                    return ch_df[keep].copy()

            pro = self._ensure_client()
            for status in statuses:
                try:
                    part = pro.stock_basic(
                        exchange="",
                        list_status=status,
                        fields="ts_code,name,list_date,delist_date,market,list_status",
                    )
                except Exception:
                    if status == "P":
                        continue
                    raise
                if part is None or part.empty:
                    continue
                frames.append(part)
            if not frames:
                return pd.DataFrame()
            out = pd.concat(frames, ignore_index=True)
            if "ts_code" in out.columns:
                out = out.drop_duplicates(subset=["ts_code"], keep="first")
            return out

        def _fetch(kw: Dict[str, Any]) -> pd.DataFrame:
            rows = []
            target_date = kw.get("date")
            need_history = target_date is not None
            include_bse = bool(kw.get("include_bse"))

            for t in kw["types"]:
                if t == "stock":
                    df = _fetch_stock_basic_frames(need_history=need_history)
                    if df is None or df.empty:
                        continue
                    if not include_bse and "ts_code" in df.columns:
                        df = df[~df["ts_code"].map(self._is_bse_ts_code)].copy()
                    df["type"] = "stock"
                elif t in ("fund", "etf", "lof"):
                    pro = self._ensure_client()
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
                    pro = self._ensure_client()
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
                return pd.DataFrame(columns=["display_name", "name", "start_date", "end_date", "type"])
            merged = pd.concat(rows, ignore_index=True).drop_duplicates("ts_code")
            if target_date is not None:
                try:
                    target_dt = pd.to_datetime(target_date)
                    start_dt = pd.to_datetime(merged["start_date"], errors="coerce").fillna(pd.Timestamp.min)
                    end_dt = pd.to_datetime(merged["end_date"], errors="coerce").fillna(pd.Timestamp.max)
                    merged = merged[(start_dt <= target_dt) & (end_dt >= target_dt)]
                except Exception:
                    pass
            merged.set_index("ts_code", inplace=True)
            merged.index = [self._to_jq_code(code) for code in merged.index]
            return merged

        # 与 jqdata 对齐：磁盘存 parquet DataFrame，进程内 mem 复用
        df = self._cache.cached_call("get_all_securities", kwargs, _fetch, result_type="df")
        if df is None or df.empty:
            empty = pd.DataFrame(columns=["display_name", "name", "start_date", "end_date", "type"])
            return self._memo_set(memo_key, empty)
        if "start_date" in df.columns:
            df["start_date"] = pd.to_datetime(df["start_date"])
        if "end_date" in df.columns:
            df["end_date"] = pd.to_datetime(df["end_date"])
        return self._memo_set(memo_key, df)

    def _fetch_index_weight_df(self, index_code: str, date: Optional[Union[str, datetime]] = None) -> pd.DataFrame:
        """
        拉取指数权重。权重多为月末更新，单日 trade_date 常空；
        直接一次区间查询取最近一期，避免「空查 + 再查」双 RTT。
        """
        target_date = self._format_date(date) or datetime.today().strftime("%Y%m%d")
        memo_key = f"idx_weight:{index_code}:{target_date}"

        def _fetch() -> pd.DataFrame:
            if self._ch_available():
                assert self._ch is not None
                ch_df = self._ch.fetch_index_weight_asof(index_code, target_date)
                if ch_df is not None and not ch_df.empty:
                    return ch_df
                # 回退旧区间扫描
                end_dt = pd.to_datetime(target_date)
                start_dt = end_dt - pd.Timedelta(days=120)
                ch_df = self._ch.fetch_index_weight(
                    index_code,
                    start_dt.strftime("%Y%m%d"),
                    target_date,
                )
                if ch_df is not None and not ch_df.empty:
                    latest = ch_df["trade_date"].max()
                    return ch_df[ch_df["trade_date"] == latest].copy()

            pro = self._ensure_client()
            end_dt = pd.to_datetime(target_date)
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
        """优先全表 map；其次单行 mem；再 CH 点查；最后拉全表建 map。"""
        hit, mapping = self._memo_get("stock_basic_map_L")
        if hit and isinstance(mapping, dict):
            return mapping.get(ts_code)

        row_key = f"stock_basic_row:{ts_code}"
        hit, cached_row = self._memo_get(row_key)
        if hit:
            return cached_row

        if self._ch_available():
            assert self._ch is not None
            row = self._ch.fetch_stock_basic_row(ts_code)
            if row:
                return self._memo_set(row_key, row)

        def _load() -> Dict[str, Dict[str, Any]]:
            df = None
            if self._ch_available():
                assert self._ch is not None
                df = self._ch.fetch_stock_basic(list_status="L")
            if df is None or df.empty:
                pro = self._ensure_client()
                df = pro.stock_basic(
                    exchange="",
                    list_status="L",
                    fields="ts_code,name,list_date,delist_date,industry,market",
                )
            if df is None or df.empty:
                return {}
            out: Dict[str, Dict[str, Any]] = {}
            for r in df.itertuples(index=False):
                out[str(r.ts_code)] = {
                    "ts_code": str(r.ts_code),
                    "name": getattr(r, "name", None),
                    "list_date": getattr(r, "list_date", None),
                    "delist_date": getattr(r, "delist_date", None),
                    "industry": getattr(r, "industry", None),
                    "market": getattr(r, "market", None),
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
        """
        映射查询日到最近交易日。
        优先：进程 mem → 磁盘点缓存 → CH LIMIT 1 → 窄窗口 trade_cal。
        避免走 get_trade_days 全量区间 + datetime 转换。
        """
        q = self._format_date(query_dt) or datetime.today().strftime("%Y%m%d")
        memo_key = f"trade_day:{q}"
        hit, last_day = self._memo_get(memo_key)
        if not hit:

            def _fetch(_kw: Dict[str, Any]) -> List[str]:
                if self._ch_available():
                    assert self._ch is not None
                    ch_day = self._ch.fetch_prev_open_day(q, exchange="SSE")
                    if ch_day:
                        return [str(ch_day)]
                end = q
                start = (pd.to_datetime(q) - pd.Timedelta(days=20)).strftime("%Y%m%d")
                try:
                    pro = self._ensure_client()
                    df = pro.trade_cal(
                        exchange="SSE",
                        start_date=start,
                        end_date=end,
                        fields="cal_date,is_open",
                    )
                    if df is not None and not df.empty:
                        open_days = df[df["is_open"] == 1]["cal_date"].astype(str).sort_values()
                        if len(open_days):
                            return [str(open_days.iloc[-1])]
                except Exception:
                    pass
                return []

            ymd_list = self._cache.cached_call(
                "get_trade_day",
                {"query_dt": q},
                _fetch,
                result_type="list_str",
            )
            ymd = ymd_list[0] if ymd_list else None
            if not ymd:
                try:
                    days = self.get_trade_days(end_date=query_dt, count=1)
                    ymd = self._format_date(days[-1]) if days else None
                except Exception:
                    ymd = None
            last_day = None
            if ymd:
                try:
                    last_day = pd.to_datetime(ymd).date()
                except Exception:
                    last_day = None
            self._memo_set(memo_key, last_day)

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

    def _daily_basic_valuation_from_raw(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["code", "market_cap", "circulating_market_cap", "pe_ratio", "pb_ratio", "day"]
            )
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

    def _daily_basic_valuation(self, trade_date: str, *, asof: bool = True) -> pd.DataFrame:
        memo_key = f"daily_basic:{trade_date}:asof={int(bool(asof))}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> pd.DataFrame:
            df = None
            if self._ch_available():
                assert self._ch is not None
                # 单日快照允许 asof；连续序列应 asof=False，缺日走远程保真
                df = self._ch.fetch_daily_basic(trade_date, asof=asof)
            if df is None or df.empty:
                pro = self._ensure_client()
                df = pro.daily_basic(
                    trade_date=trade_date,
                    fields="ts_code,trade_date,total_mv,circ_mv,pe,pb",
                )
            return self._daily_basic_valuation_from_raw(
                df if df is not None else pd.DataFrame()
            )

        out = self._cache.cached_call(
            "daily_basic_valuation",
            {"trade_date": trade_date, "asof": int(bool(asof))},
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

        frames: List[pd.DataFrame] = []
        missing: List[str] = []

        # CH：一次拉区间，避免逐日空查 RTT
        ch_by_day: Dict[str, pd.DataFrame] = {}
        if self._ch_available():
            assert self._ch is not None
            try:
                raw = self._ch.fetch_daily_basic_range(min(trade_dates), max(trade_dates))
                if raw is not None and not raw.empty and "trade_date" in raw.columns:
                    raw = raw.copy()
                    raw["_d"] = raw["trade_date"].astype(str).str.replace("-", "", regex=False).str[:8]
                    for d, part in raw.groupby("_d"):
                        valued = self._daily_basic_valuation_from_raw(part.drop(columns=["_d"]))
                        ch_by_day[str(d)] = valued
                        self._memo_set(f"daily_basic:{d}:asof=0", valued)
            except Exception:
                ch_by_day = {}

        for d in trade_dates:
            hit, cached = self._memo_get(f"daily_basic:{d}:asof=0")
            if hit and cached is not None and not getattr(cached, "empty", False):
                frames.append(cached)
            elif d in ch_by_day and not ch_by_day[d].empty:
                frames.append(ch_by_day[d])
            else:
                missing.append(d)

        if missing:
            max_workers = min(4, len(missing))
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futs = {
                        pool.submit(self._daily_basic_valuation, d, asof=False): d
                        for d in missing
                    }
                    for fut in as_completed(futs):
                        part = fut.result()
                        if part is not None and not part.empty:
                            frames.append(part)
            except Exception:
                for d in missing:
                    part = self._daily_basic_valuation(d, asof=False)
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

    def _sw_industry_index(self) -> Dict[str, List[str]]:
        """行业码 → 成分股（jq 后缀）倒排索引；构建一次后 mem 复用。"""
        memo_key = "sw_industry_index"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        df = self._sw_member_table()
        index: Dict[str, List[str]] = {}
        if df is None or df.empty:
            return self._memo_set(memo_key, index)

        work = df
        if "is_new" in work.columns:
            cur = work[work["is_new"].astype(str).str.upper() == "Y"]
            if not cur.empty:
                work = cur
        member_col = "ts_code" if "ts_code" in work.columns else ("con_code" if "con_code" in work.columns else None)
        if not member_col:
            return self._memo_set(memo_key, index)

        members = (
            work[member_col]
            .dropna()
            .astype(str)
            .str.replace(".SH", ".XSHG", regex=False)
            .str.replace(".SZ", ".XSHE", regex=False)
        )
        work = work.loc[members.index]
        members = members.tolist()
        for col in ("l1_code", "l2_code", "l3_code", "index_code"):
            if col not in work.columns:
                continue
            keys = work[col].astype(str).tolist()
            for key, mem in zip(keys, members):
                if not key or key.lower() in {"nan", "none"}:
                    continue
                bucket = index.setdefault(key, [])
                bucket.append(mem)
        # 去重保序
        for key, vals in list(index.items()):
            seen = set()
            uniq: List[str] = []
            for v in vals:
                if v in seen:
                    continue
                seen.add(v)
                uniq.append(v)
            index[key] = uniq
        return self._memo_set(memo_key, index)

    def get_industry_stocks(self, industry_code: str, date: Optional[Union[str, datetime]] = None) -> List[str]:
        """行业成分股：申万代码本地倒排 + 按码磁盘缓存。"""
        _ = date
        code = str(industry_code or "").strip()
        if code.upper().startswith("HY"):
            def _sw_l1_first() -> str:
                def _fetch_classify(_kw: Dict[str, Any]) -> List[str]:
                    try:
                        pro = self._ensure_client()
                        classify = pro.index_classify(level="L1", src="SW2021")
                        if classify is not None and not classify.empty:
                            val = str(classify.iloc[0].get("index_code") or "")
                            return [val] if val else []
                    except Exception:
                        return []
                    return []

                hit, cached = self._memo_get("sw_l1_first")
                if hit:
                    return str(cached or "")
                rows = self._cache.cached_call("sw_l1_first", {}, _fetch_classify, result_type="list_str")
                val = rows[0] if rows else ""
                self._memo_set("sw_l1_first", val)
                return val

            code = _sw_l1_first()
        if not code:
            return []

        memo_key = f"industry_stocks:{code}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return list(cached)

        def _fetch(_kw: Dict[str, Any]) -> List[str]:
            idx = self._sw_industry_index()
            return list(idx.get(code, []))

        out = self._cache.cached_call(
            "get_industry_stocks",
            {"industry_code": code},
            _fetch,
            result_type="list_str",
        )
        return self._memo_set(memo_key, list(out or []))

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

    def _concept_flag_path(self) -> Optional[Path]:
        if not getattr(self._cache, "enabled", False) or not self._cache.cache_dir:
            return None
        return Path(self._cache.cache_dir) / "concept_api_enabled.json"

    def _concept_api_enabled(self) -> bool:
        hit, val = self._memo_get("concept_api_enabled")
        if hit:
            return bool(val)
        path = self._concept_flag_path()
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                enabled = bool(data.get("enabled", True))
                self._memo_set("concept_api_enabled", enabled)
                return enabled
            except Exception:
                pass
        # 未知时先当作可用，真正调用失败后再标记
        return True

    def _mark_concept_api(self, enabled: bool) -> None:
        self._memo_set("concept_api_enabled", bool(enabled))
        path = self._concept_flag_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "enabled": bool(enabled),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

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
        hit, cached = self._memo_get(memo_key)
        if hit:
            return list(cached)

        def _fetch(_kw: Dict[str, Any]) -> List[str]:
            if not self._concept_api_enabled():
                return []
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

        out = self._cache.cached_call(
            "get_concept_stocks",
            {"concept_code": code},
            _fetch,
            result_type="list_str",
        )
        return self._memo_set(memo_key, list(out or []))

    def get_concept(self, security: Union[str, List[str]], date: Optional[Union[str, datetime]] = None) -> Any:
        _ = date
        securities = list(security if isinstance(security, (list, tuple, set)) else [security])
        empty = {str(sec): {"jq_concept": []} for sec in securities}
        if not self._concept_api_enabled():
            return empty

        result: Dict[str, Any] = {}
        for sec in securities:
            ts_code = self._to_ts_code(str(sec))
            memo_key = f"concept:{ts_code}"
            hit, cached = self._memo_get(memo_key)
            if hit:
                result[str(sec)] = {"jq_concept": list(cached)}
                continue

            def _fetch(_kw: Dict[str, Any], code: str = ts_code) -> List[Dict[str, Any]]:
                if not self._concept_api_enabled():
                    return []
                pro = self._ensure_client()
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

            concepts = self._cache.cached_call(
                "get_concept",
                {"ts_code": ts_code},
                _fetch,
                result_type="list_dict",
            )
            concepts = list(concepts or [])
            self._memo_set(memo_key, concepts)
            result[str(sec)] = {"jq_concept": concepts}
        return result

    def get_fund_info(self, security: str, date: Optional[Union[str, datetime]] = None) -> Any:
        _ = date
        ts_code = self._to_ts_code(security)
        if str(security).upper().endswith(".OF") and not ts_code.upper().endswith(".OF"):
            ts_code = f"{security.split('.')[0]}.OF"
        elif not ts_code.upper().endswith((".OF", ".SH", ".SZ")):
            ts_code = f"{ts_code}.OF"
        memo_key = f"fund_info:{ts_code}"
        hit, cached = self._memo_get(memo_key)
        if hit:
            return cached

        def _fetch(_kw: Dict[str, Any]) -> List[Dict[str, Any]]:
            df = None
            if self._ch_available():
                assert self._ch is not None
                df = self._ch.fetch_fund_basic(ts_code)
            if df is None or df.empty:
                pro = self._ensure_client()
                fields = "ts_code,name,fund_type,found_date,list_date,delist_date,management,custodian"
                if ts_code.upper().endswith(".OF"):
                    trials = [{"ts_code": ts_code, "market": "O", "fields": fields}]
                else:
                    trials = [{"ts_code": ts_code, "market": "E", "fields": fields}]
                try:
                    for kwargs in trials:
                        try:
                            df = pro.fund_basic(**kwargs)
                        except TypeError:
                            kwargs.pop("fields", None)
                            df = pro.fund_basic(**kwargs)
                        if df is not None and not df.empty:
                            break
                except Exception as exc:
                    raise RuntimeError(f"get_fund_info 失败: {exc}") from exc
            if df is None or df.empty:
                return []
            row = df.iloc[0].to_dict()
            info = {
                "fund_name": row.get("name"),
                "fund_type": row.get("fund_type"),
                "start_date": row.get("found_date") or row.get("list_date"),
                "end_date": row.get("delist_date"),
                "advisor": row.get("management"),
                "trustee": row.get("custodian"),
                "raw": row,
            }
            return [info]

        rows = self._cache.cached_call(
            "get_fund_info",
            {"ts_code": ts_code},
            _fetch,
            result_type="list_dict",
        )
        out = rows[0] if rows else {}
        return self._memo_set(memo_key, out)

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
            # 统一到聚宽后缀，并把北交所 821/83x/87x/43x 映射到 920
            codes = [self._to_jq_code(str(c)) for c in df["ts_code"].dropna().astype(str).tolist()]
            return sorted(set(codes))

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
        # 与融资标的同源接口（margin_secs），直接复用记忆缓存；与 jq 比时北交所可不一致
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
