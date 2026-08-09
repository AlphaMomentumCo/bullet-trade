# -*- coding: utf-8 -*-
"""
小市值策略：聚宽原生接口 vs 优化后 bullet-trade 分环节精细耗时剖析。

用法（在沙箱根目录）:
  C:\\Users\\90405\\anaconda3\\envs\\quant\\python.exe my_strategies\\bench_xiaoshizhi_profile.py --mode all

输出:
  my_results/xiaoshizhi_profile/
    summary_*.csv / *.json
    calls_*.csv          # 每次调用明细
    stages_*.csv         # 引擎/策略阶段汇总
    daily_*.csv          # 按交易日拆解
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SANDBOX_ROOT = Path(r"C:\Users\90405\Documents\bullet-trade-sandbox-gp")
MAIN_ROOT = Path(r"C:\Users\90405\Documents\bullet-trade")
MAIN_ENV = MAIN_ROOT / ".env"
OUT_DIR = SANDBOX_ROOT / "my_results" / "xiaoshizhi_profile"
PYTHON = Path(r"C:\Users\90405\anaconda3\envs\quant\python.exe")
STRATEGY = SANDBOX_ROOT / "my_strategies" / "小市值策略.py"

# 落在 JQ 试用权限窗口内；约一个月交易日，兼顾完整性与可跑完
BT_START = "2026-04-01"
BT_END = "2026-04-30"


class CallProfiler:
    """高精度调用剖析器：记录每次调用墙钟、参数摘要、所属交易日。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._day: Optional[str] = None
        self._phase: str = ""
        self._enabled = True

    def set_day(self, day: Any) -> None:
        if day is None:
            self._day = None
            return
        try:
            self._day = str(pd_to_date(day))
        except Exception:
            self._day = str(day)

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        profiler = self

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not profiler._enabled:
                return fn(*args, **kwargs)
            t0 = time.perf_counter()
            err = None
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                dt = time.perf_counter() - t0
                profiler.calls.append(
                    {
                        "api": name,
                        "seconds": round(dt, 6),
                        "day": profiler._day,
                        "phase": profiler._phase,
                        "ok": err is None,
                        "error": err,
                        "args_preview": _preview_args(args, kwargs),
                        "ts": datetime.now().isoformat(timespec="milliseconds"),
                    }
                )

        wrapped.__name__ = getattr(fn, "__name__", name)
        wrapped.__doc__ = getattr(fn, "__doc__", None)
        return wrapped

    def aggregate(self) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[float]] = defaultdict(list)
        err_count: Dict[str, int] = defaultdict(int)
        for c in self.calls:
            buckets[c["api"]].append(float(c["seconds"]))
            if not c.get("ok", True):
                err_count[c["api"]] += 1
        rows = []
        for api, xs in sorted(buckets.items(), key=lambda kv: -sum(kv[1])):
            rows.append(_stats_row(api, xs, errors=err_count.get(api, 0)))
        return rows

    def by_day(self) -> List[Dict[str, Any]]:
        day_api: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for c in self.calls:
            day = c.get("day") or "_none_"
            day_api[(day, c["api"])].append(float(c["seconds"]))
        rows = []
        for (day, api), xs in sorted(day_api.items()):
            row = _stats_row(api, xs)
            row["day"] = day
            rows.append(row)
        return rows


def pd_to_date(value: Any):
    import pandas as pd

    return pd.to_datetime(value).date()


def _preview_args(args: Sequence[Any], kwargs: Dict[str, Any]) -> str:
    parts = []
    for a in args[:2]:
        parts.append(_short(a))
    for k in ("security", "start_date", "end_date", "count", "frequency", "fq", "date"):
        if k in kwargs:
            parts.append(f"{k}={_short(kwargs[k])}")
    return "; ".join(parts)[:240]


def _short(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        return f"list(n={len(seq)})"
    s = str(value)
    return s if len(s) <= 60 else s[:57] + "..."


def _stats_row(api: str, xs: List[float], errors: int = 0) -> Dict[str, Any]:
    xs_sorted = sorted(xs)
    n = len(xs_sorted)

    def pct(p: float) -> float:
        if n == 0:
            return 0.0
        idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
        return xs_sorted[idx]

    return {
        "api": api,
        "calls": n,
        "errors": errors,
        "total_s": round(sum(xs_sorted), 6),
        "mean_s": round(statistics.mean(xs_sorted), 6) if n else 0.0,
        "p50_s": round(pct(50), 6),
        "p95_s": round(pct(95), 6),
        "min_s": round(xs_sorted[0], 6) if n else 0.0,
        "max_s": round(xs_sorted[-1], 6) if n else 0.0,
        "share_of_api_total_pct": None,  # filled later
    }


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if MAIN_ENV.exists():
        load_dotenv(str(MAIN_ENV), override=False)


def _jq_auth() -> Any:
    import jqdatasdk as jq

    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        raise RuntimeError("缺少 JQDATA_USERNAME/PASSWORD")
    jq.auth(user, pwd)
    return jq


# ---------------------------------------------------------------------------
# Mode: jq native — 按小市值策略日程模拟数据层（不含撮合引擎）
# ---------------------------------------------------------------------------
def run_jq_native_profile() -> Dict[str, Any]:
    print("\n========== [jq_native] 聚宽原生数据层（对齐小市值策略日程） ==========")
    _load_dotenv()
    jq = _jq_auth()
    prof = CallProfiler()

    get_trade_days = prof.wrap("jq.get_trade_days", jq.get_trade_days)
    get_fundamentals = prof.wrap("jq.get_fundamentals", jq.get_fundamentals)
    get_price = prof.wrap("jq.get_price", jq.get_price)
    # 选股 query 构造本身极快，仍单独计时
    query_fn = jq.query
    valuation = jq.valuation

    wall0 = time.perf_counter()
    stage_times: Dict[str, float] = defaultdict(float)
    counters: Dict[str, int] = {}

    t = time.perf_counter()
    trade_days = list(get_trade_days(start_date=BT_START, end_date=BT_END))
    stage_times["01_load_trade_days"] += time.perf_counter() - t
    print(f"  交易日数: {len(trade_days)}  {BT_START} .. {BT_END}")

    stocknum = 3
    refresh_rate = 5
    days_counter = 0
    # 仅模拟持仓代码集合，用于决定是否走调仓日逻辑
    positions: List[str] = []

    for i, day in enumerate(trade_days):
        day_d = pd_to_date(day)
        prof.set_day(day_d)
        day_t0 = time.perf_counter()

        # 对齐策略: run_daily(trade, 'open') — 每个交易日开盘
        if days_counter % refresh_rate == 0:
            # --- 卖出侧：原策略对持仓 order_target_value；数据层通常再取价 ---
            prof.set_phase("rebalance_sell_price")
            t = time.perf_counter()
            for stock in list(positions):
                get_price(
                    stock,
                    end_date=datetime.combine(day_d, datetime.min.time().replace(hour=9, minute=30)),
                    frequency="daily",
                    fields=["open", "close"],
                    count=1,
                    fq="pre",
                )
            stage_times["02_rebalance_sell_get_price"] += time.perf_counter() - t

            # --- 选股 get_fundamentals ---
            prof.set_phase("check_stocks_fundamentals")
            t = time.perf_counter()
            q = (
                query_fn(valuation.code, valuation.market_cap)
                .filter(valuation.market_cap.between(20, 30))
                .order_by(valuation.market_cap.asc())
            )
            df = get_fundamentals(q, date=day_d)
            stage_times["03_get_fundamentals"] += time.perf_counter() - t

            buylist = list(df["code"]) if df is not None and not df.empty else []

            # --- filter_paused_stock: 与策略一致，对 fundamentals 全部候选查停牌，再取前 3 ---
            # 聚宽 SDK 无完整 current_data，用 get_price(count=1, fields含paused) 对齐框架路径
            prof.set_phase("filter_paused_current_data")
            t = time.perf_counter()
            kept = []
            end_dt = datetime.combine(day_d, datetime.min.time().replace(hour=9, minute=30))
            for stock in buylist:
                try:
                    pdf = get_price(
                        stock,
                        end_date=end_dt,
                        frequency="daily",
                        fields=["open", "close", "paused", "high_limit", "low_limit"],
                        count=1,
                        fq="pre",
                    )
                    paused = False
                    if pdf is not None and not pdf.empty:
                        row = pdf.iloc[-1]
                        if "paused" in pdf.columns:
                            paused = bool(row.get("paused", False))
                    if not paused:
                        kept.append(stock)
                except Exception:
                    continue
            stock_list = kept[:stocknum]
            stage_times["04_filter_paused_get_price"] += time.perf_counter() - t
            counters["filter_paused_candidate_checks"] = counters.get(
                "filter_paused_candidate_checks", 0
            ) + len(buylist)
            counters["rebalance_days"] = counters.get("rebalance_days", 0) + 1

            # --- 买入取价（撮合前再取开盘价）---
            prof.set_phase("rebalance_buy_price")
            t = time.perf_counter()
            for stock in stock_list:
                get_price(
                    stock,
                    end_date=end_dt,
                    frequency="daily",
                    fields=["open", "close"],
                    count=1,
                    fq="pre",
                )
            stage_times["05_rebalance_buy_get_price"] += time.perf_counter() - t

            positions = list(stock_list)
            days_counter = 1
        else:
            days_counter += 1

        # --- 日终估值：持仓收盘价（框架 _update_positions）---
        prof.set_phase("day_end_mark")
        t = time.perf_counter()
        close_dt = datetime.combine(day_d, datetime.min.time().replace(hour=15, minute=0))
        for stock in positions:
            get_price(
                stock,
                end_date=close_dt,
                frequency="daily",
                fields=["close"],
                count=1,
                fq="none",
            )
        stage_times["06_day_end_mark_get_price"] += time.perf_counter() - t

        stage_times["07_per_day_wall"] += time.perf_counter() - day_t0
        if (i + 1) % 5 == 0 or i == 0 or i == len(trade_days) - 1:
            print(f"  day {i+1}/{len(trade_days)} {day_d} positions={positions}")

    wall = time.perf_counter() - wall0
    stage_times["00_total_wall"] = wall

    api_rows = prof.aggregate()
    _fill_share(api_rows)
    return {
        "env": "jq_native",
        "note": (
            "数据层日程模拟，严格对齐小市值策略："
            "调仓日 get_fundamentals → 对全部候选 get_price 查停牌 → 取前3；"
            "调仓买卖取价 + 每日持仓日终估值；不含 BT 撮合/下单引擎开销"
        ),
        "start": BT_START,
        "end": BT_END,
        "trade_days": len(trade_days),
        "wall_seconds": round(wall, 6),
        "stages": dict(stage_times),
        "counters": counters,
        "api_agg": api_rows,
        "api_by_day": prof.by_day(),
        "calls": prof.calls,
        "session_manifest": None,
    }


# ---------------------------------------------------------------------------
# Mode: optimized BT — 完整回测 + 猴子补丁剖析
# ---------------------------------------------------------------------------
def run_bt_optimized_profile() -> Dict[str, Any]:
    print("\n========== [bt_optimized] 优化后 bullet-trade 完整回测剖析 ==========")
    # 确保沙箱包优先
    sand = str(SANDBOX_ROOT)
    main = str(MAIN_ROOT)
    while sand in sys.path:
        sys.path.remove(sand)
    sys.path.insert(0, sand)
    if main in sys.path:
        sys.path.remove(main)

    _load_dotenv()
    os.environ["BT_ENV_FILE"] = str(MAIN_ENV)
    os.environ["DEFAULT_DATA_PROVIDER"] = "jqdata"
    os.environ["BT_BACKTEST_DATA_SESSION"] = "true"
    os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"

    cache_dir = OUT_DIR / "cache_bt_optimized"
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["DATA_CACHE_DIR"] = str(cache_dir)

    from bullet_trade.utils.env_loader import load_env

    load_env(str(MAIN_ENV), verbose=False, override=False)

    import bullet_trade.data.api as api_mod
    from bullet_trade.core import engine as engine_mod
    from bullet_trade.core.engine import BacktestEngine, create_backtest

    prof = CallProfiler()
    stage_times: Dict[str, float] = defaultdict(float)

    # --- patch data APIs ---
    api_mod.get_price = prof.wrap("bt.get_price", api_mod.get_price)
    api_mod.get_fundamentals = prof.wrap("bt.get_fundamentals", api_mod.get_fundamentals)
    api_mod.get_current_data = prof.wrap("bt.get_current_data", api_mod.get_current_data)
    api_mod.get_trade_days = prof.wrap("bt.get_trade_days", api_mod.get_trade_days)
    if hasattr(api_mod, "get_security_info"):
        api_mod.get_security_info = prof.wrap("bt.get_security_info", api_mod.get_security_info)

    # provider 底层也包一层，区分「框架入口」与「真实远程/缓存」
    provider = api_mod.get_data_provider()
    if hasattr(provider, "get_price"):
        provider.get_price = prof.wrap("provider.get_price", provider.get_price)
    if hasattr(provider, "get_fundamentals"):
        provider.get_fundamentals = prof.wrap(
            "provider.get_fundamentals", provider.get_fundamentals
        )

    # --- patch engine hot methods ---
    _orig_run_day = BacktestEngine._run_trading_day
    _orig_process = BacktestEngine._process_orders
    _orig_update_pos = BacktestEngine._update_positions
    _orig_resolve = BacktestEngine._resolve_base_exec_price
    _orig_record = BacktestEngine._record_daily

    def _run_day(self, trade_day, market_periods):  # type: ignore[no-untyped-def]
        prof.set_day(trade_day)
        prof.set_phase("engine._run_trading_day")
        t0 = time.perf_counter()
        try:
            return _orig_run_day(self, trade_day, market_periods)
        finally:
            stage_times["engine._run_trading_day"] += time.perf_counter() - t0

    def _process(self, current_dt):  # type: ignore[no-untyped-def]
        prof.set_phase("engine._process_orders")
        t0 = time.perf_counter()
        try:
            return _orig_process(self, current_dt)
        finally:
            dt = time.perf_counter() - t0
            stage_times["engine._process_orders"] += dt
            prof.calls.append(
                {
                    "api": "engine._process_orders",
                    "seconds": round(dt, 6),
                    "day": prof._day,
                    "phase": "engine._process_orders",
                    "ok": True,
                    "error": None,
                    "args_preview": f"dt={current_dt}",
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                }
            )

    def _update_pos(self):  # type: ignore[no-untyped-def]
        prof.set_phase("engine._update_positions")
        t0 = time.perf_counter()
        try:
            return _orig_update_pos(self)
        finally:
            dt = time.perf_counter() - t0
            stage_times["engine._update_positions"] += dt
            prof.calls.append(
                {
                    "api": "engine._update_positions",
                    "seconds": round(dt, 6),
                    "day": prof._day,
                    "phase": "engine._update_positions",
                    "ok": True,
                    "error": None,
                    "args_preview": "",
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                }
            )

    def _resolve(self, security, current_dt, fq_mode, *, hint_last_price=None):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return _orig_resolve(
                self, security, current_dt, fq_mode, hint_last_price=hint_last_price
            )
        finally:
            dt = time.perf_counter() - t0
            stage_times["engine._resolve_base_exec_price"] += dt
            prof.calls.append(
                {
                    "api": "engine._resolve_base_exec_price",
                    "seconds": round(dt, 6),
                    "day": prof._day,
                    "phase": "engine._resolve_base_exec_price",
                    "ok": True,
                    "error": None,
                    "args_preview": f"sec={security}; hint={'Y' if hint_last_price else 'N'}",
                    "ts": datetime.now().isoformat(timespec="milliseconds"),
                }
            )

    def _record(self):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return _orig_record(self)
        finally:
            stage_times["engine._record_daily"] += time.perf_counter() - t0

    BacktestEngine._run_trading_day = _run_day  # type: ignore[method-assign]
    BacktestEngine._process_orders = _process  # type: ignore[method-assign]
    BacktestEngine._update_positions = _update_pos  # type: ignore[method-assign]
    BacktestEngine._resolve_base_exec_price = _resolve  # type: ignore[method-assign]
    BacktestEngine._record_daily = _record  # type: ignore[method-assign]

    # 同步 patch engine 模块内 api_get_price（撮合用）
    engine_mod.api_get_price = prof.wrap("engine.api_get_price", engine_mod.api_get_price)

    out = OUT_DIR / "bt_optimized_run"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    session_cfg = {
        "enabled": True,
        "price_block_cache_enabled": True,
        "manifest_path": str(out / "session_manifest.json"),
    }

    wall0 = time.perf_counter()
    prof.set_phase("create_backtest")
    results = create_backtest(
        strategy_file=str(STRATEGY),
        start_date=BT_START,
        end_date=BT_END,
        frequency="day",
        initial_cash=1_000_000,
        benchmark="000300.XSHG",
        log_file=str(out / "backtest.log"),
        data_session_config=session_cfg,
    )
    wall = time.perf_counter() - wall0
    stage_times["00_total_wall"] = wall

    runtime = None
    if isinstance(results, dict):
        runtime = (results.get("meta") or {}).get("runtime_seconds")
        manifest = results.get("backtest_data_session")
    else:
        manifest = None

    # restore (best-effort; process will exit)
    BacktestEngine._run_trading_day = _orig_run_day  # type: ignore[method-assign]
    BacktestEngine._process_orders = _orig_process  # type: ignore[method-assign]
    BacktestEngine._update_positions = _orig_update_pos  # type: ignore[method-assign]
    BacktestEngine._resolve_base_exec_price = _orig_resolve  # type: ignore[method-assign]
    BacktestEngine._record_daily = _orig_record  # type: ignore[method-assign]

    api_rows = prof.aggregate()
    _fill_share(api_rows)

    # BacktestCurrentData.__getitem__ 单独统计：从 calls 里用 current_data 次数推
    # get_current_data 返回容器，真正耗时在 __getitem__，已由 provider.get_price / bt.get_price 体现

    print(f"  wall={wall:.3f}s runtime_meta={runtime}")
    return {
        "env": "bt_optimized",
        "note": "完整回测 + API/引擎方法猴子补丁；会话行情块开启；独立磁盘缓存",
        "start": BT_START,
        "end": BT_END,
        "trade_days": None,
        "wall_seconds": round(wall, 6),
        "runtime_seconds_meta": runtime,
        "stages": dict(stage_times),
        "api_agg": api_rows,
        "api_by_day": prof.by_day(),
        "calls": prof.calls,
        "session_manifest": manifest,
        "result_metrics": _safe_metrics(results),
    }


def _safe_metrics(results: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(results, dict):
        return None
    metrics = results.get("metrics") or {}
    meta = results.get("meta") or {}
    keep = {}
    for k in (
        "total_returns",
        "annual_returns",
        "max_drawdown",
        "sharpe",
        "runtime_seconds",
    ):
        if k in metrics:
            keep[k] = metrics[k]
        if k in meta:
            keep[k] = meta[k]
    return keep or None


def _fill_share(api_rows: List[Dict[str, Any]]) -> None:
    total = sum(float(r["total_s"]) for r in api_rows) or 1.0
    for r in api_rows:
        r["share_of_api_total_pct"] = round(100.0 * float(r["total_s"]) / total, 2)


# ---------------------------------------------------------------------------
# IO / orchestration
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # union keys
    keys: List[str] = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _stages_to_rows(env: str, stages: Dict[str, float]) -> List[Dict[str, Any]]:
    total = float(stages.get("00_total_wall") or sum(stages.values()) or 1.0)
    rows = []
    for name, sec in sorted(stages.items(), key=lambda kv: -float(kv[1])):
        rows.append(
            {
                "env": env,
                "stage": name,
                "seconds": round(float(sec), 6),
                "share_of_wall_pct": round(100.0 * float(sec) / total, 2),
            }
        )
    return rows


def persist_env_result(payload: Dict[str, Any], stamp: str) -> Dict[str, Path]:
    env = payload["env"]
    base = OUT_DIR / stamp
    base.mkdir(parents=True, exist_ok=True)

    paths = {
        "summary_json": base / f"{env}_summary.json",
        "api_agg_csv": base / f"{env}_api_aggregate.csv",
        "api_by_day_csv": base / f"{env}_api_by_day.csv",
        "calls_csv": base / f"{env}_calls.csv",
        "stages_csv": base / f"{env}_stages.csv",
    }

    summary = {
        k: v
        for k, v in payload.items()
        if k not in ("calls", "api_by_day")
    }
    # api_agg kept in summary
    paths["summary_json"].write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    _write_csv(paths["api_agg_csv"], payload.get("api_agg") or [])
    _write_csv(paths["api_by_day_csv"], payload.get("api_by_day") or [])
    _write_csv(paths["calls_csv"], payload.get("calls") or [])
    _write_csv(paths["stages_csv"], _stages_to_rows(env, payload.get("stages") or {}))
    return paths


def print_report(payloads: Sequence[Dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print("小市值策略耗时剖析报告")
    print(f"区间: {BT_START} .. {BT_END}")
    print("=" * 100)
    for p in payloads:
        print(f"\n### 环境: {p['env']}")
        print(f"说明: {p.get('note')}")
        print(f"总墙钟: {p.get('wall_seconds')} s", end="")
        if p.get("runtime_seconds_meta") is not None:
            print(f"  | 引擎 meta.runtime_seconds: {p.get('runtime_seconds_meta')}", end="")
        print()
        print("\n[阶段耗时 stage]")
        rows = _stages_to_rows(p["env"], p.get("stages") or {})
        print(f"{'stage':<42} {'seconds':>10} {'share%':>8}")
        for r in rows[:20]:
            print(f"{r['stage']:<42} {r['seconds']:>10.4f} {r['share_of_wall_pct']:>7.2f}%")
        print("\n[API 汇总 api_aggregate] 按 total 降序")
        print(
            f"{'api':<36} {'calls':>7} {'total_s':>10} {'mean_s':>10} "
            f"{'p50_s':>10} {'p95_s':>10} {'max_s':>10} {'share%':>7}"
        )
        for r in (p.get("api_agg") or [])[:25]:
            print(
                f"{r['api']:<36} {r['calls']:>7} {r['total_s']:>10.4f} {r['mean_s']:>10.4f} "
                f"{r['p50_s']:>10.4f} {r['p95_s']:>10.4f} {r['max_s']:>10.4f} "
                f"{r.get('share_of_api_total_pct') or 0:>6.1f}%"
            )
        man = p.get("session_manifest")
        if isinstance(man, dict):
            stats = man.get("stats") or {}
            print("\n[回测数据会话 session]")
            print(
                f"  cache_hits={stats.get('cache_hits')} cache_misses={stats.get('cache_misses')} "
                f"cache_writes={stats.get('cache_writes')} "
                f"current_bar_hits={stats.get('current_bar_hits')} "
                f"current_bar_misses={stats.get('current_bar_misses')}"
            )
        if p.get("result_metrics"):
            print(f"\n[回测指标] {p['result_metrics']}")


def _spawn(mode: str) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    partial = OUT_DIR / f"_partial_{mode}.json"
    if partial.exists():
        partial.unlink()
    env = os.environ.copy()
    env["BT_ENV_FILE"] = str(MAIN_ENV)
    if mode == "bt":
        env["PYTHONPATH"] = str(SANDBOX_ROOT)
        env["BT_BACKTEST_DATA_SESSION"] = "true"
        env["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
        cache_dir = OUT_DIR / "cache_bt_optimized"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["DATA_CACHE_DIR"] = str(cache_dir)
    else:
        env["PYTHONPATH"] = str(SANDBOX_ROOT)
        cache_dir = OUT_DIR / "cache_jq_native"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # jq native 不走 BT 磁盘缓存，但清空以免误导
        env["DATA_CACHE_DIR"] = str(cache_dir)

    cmd = [
        str(PYTHON if PYTHON.exists() else sys.executable),
        str(SANDBOX_ROOT / "my_strategies" / "bench_xiaoshizhi_profile.py"),
        "--mode",
        mode,
        "--write-partial",
        str(partial),
    ]
    print(f"\n>> spawn {mode}")
    proc = subprocess.run(cmd, cwd=str(SANDBOX_ROOT), env=env, check=False, timeout=1800)
    if proc.returncode != 0:
        print(f"!! {mode} exit={proc.returncode}")
    if not partial.exists():
        return {
            "env": mode,
            "wall_seconds": 0,
            "stages": {},
            "api_agg": [],
            "api_by_day": [],
            "calls": [],
            "note": f"subprocess failed rc={proc.returncode}",
            "error": True,
        }
    return json.loads(partial.read_text(encoding="utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["jq", "bt", "all"],
        default="all",
        help="jq=聚宽原生数据层模拟; bt=优化后完整回测; all=子进程依次跑两者",
    )
    parser.add_argument("--write-partial", type=str, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode == "all":
        payloads = [_spawn("jq"), _spawn("bt")]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        all_paths = {}
        for p in payloads:
            all_paths[p.get("env", "unknown")] = {
                k: str(v) for k, v in persist_env_result(p, stamp).items()
            }
        # combined summary
        combo = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "start": BT_START,
            "end": BT_END,
            "envs": [
                {
                    "env": p.get("env"),
                    "wall_seconds": p.get("wall_seconds"),
                    "runtime_seconds_meta": p.get("runtime_seconds_meta"),
                    "note": p.get("note"),
                    "stages": p.get("stages"),
                    "api_agg": p.get("api_agg"),
                    "session_manifest": p.get("session_manifest"),
                    "result_metrics": p.get("result_metrics"),
                }
                for p in payloads
            ],
            "paths": all_paths,
        }
        latest = OUT_DIR / "summary_latest.json"
        stamped = OUT_DIR / stamp / "summary_all.json"
        text = json.dumps(combo, ensure_ascii=False, indent=2, default=str)
        latest.write_text(text, encoding="utf-8")
        stamped.write_text(text, encoding="utf-8")

        # comparison table csv
        comp_rows = []
        for p in payloads:
            for r in p.get("api_agg") or []:
                comp_rows.append({"env": p.get("env"), **r})
        _write_csv(OUT_DIR / stamp / "compare_api_aggregate.csv", comp_rows)
        _write_csv(OUT_DIR / "compare_api_aggregate_latest.csv", comp_rows)
        stage_rows = []
        for p in payloads:
            stage_rows.extend(_stages_to_rows(p.get("env") or "", p.get("stages") or {}))
        _write_csv(OUT_DIR / stamp / "compare_stages.csv", stage_rows)
        _write_csv(OUT_DIR / "compare_stages_latest.csv", stage_rows)

        print_report(payloads)
        print(f"\n结果目录: {OUT_DIR / stamp}")
        print(f"汇总: {latest}")
        return 0

    try:
        if args.mode == "jq":
            payload = run_jq_native_profile()
        else:
            payload = run_bt_optimized_profile()
    except Exception:
        traceback.print_exc()
        payload = {
            "env": "jq_native" if args.mode == "jq" else "bt_optimized",
            "wall_seconds": 0,
            "stages": {},
            "api_agg": [],
            "api_by_day": [],
            "calls": [],
            "note": "failed",
            "error": traceback.format_exc(),
        }

    if args.write_partial:
        # calls 可能很大，完整写入
        Path(args.write_partial).write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return 0 if not payload.get("error") else 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    persist_env_result(payload, stamp)
    print_report([payload])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
