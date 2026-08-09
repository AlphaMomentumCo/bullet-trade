# -*- coding: utf-8 -*-
"""
打板策略：聚宽原生 vs 优化后 bullet-trade 分环节精细耗时剖析。

使用包装策略 打板策略_bench_profile.py：
  - max_universe_size=200, max_watchlist_size=80（原版 2000 全市场点查过慢）
  - entry_start=09:30（日频开盘 bar 才能进入扫描）

用法:
  python my_strategies/bench_daban_profile.py --mode all
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
OUT_DIR = SANDBOX_ROOT / "my_results" / "daban_profile"
PYTHON = Path(r"C:\Users\90405\anaconda3\envs\quant\python.exe")
STRATEGY = SANDBOX_ROOT / "my_strategies" / "打板策略_bench_profile.py"

BT_START = "2026-04-07"
BT_END = "2026-04-17"
MAX_UNIVERSE = 200
MAX_WATCHLIST = 80
BENCHMARK = "000300.XSHG"


class CallProfiler:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._day: Optional[str] = None
        self._phase: str = ""

    def set_day(self, day: Any) -> None:
        if day is None:
            self._day = None
            return
        try:
            import pandas as pd

            self._day = str(pd.to_datetime(day).date())
        except Exception:
            self._day = str(day)

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        profiler = self

        def wrapped(*args: Any, **kwargs: Any) -> Any:
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

        return wrapped

    def aggregate(self) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[float]] = defaultdict(list)
        errs: Dict[str, int] = defaultdict(int)
        for c in self.calls:
            buckets[c["api"]].append(float(c["seconds"]))
            if not c.get("ok", True):
                errs[c["api"]] += 1
        rows = [_stats_row(api, xs, errs.get(api, 0)) for api, xs in sorted(buckets.items(), key=lambda kv: -sum(kv[1]))]
        _fill_share(rows)
        return rows

    def by_day(self) -> List[Dict[str, Any]]:
        day_api: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for c in self.calls:
            day_api[(c.get("day") or "_none_", c["api"])].append(float(c["seconds"]))
        rows = []
        for (day, api), xs in sorted(day_api.items()):
            row = _stats_row(api, xs)
            row["day"] = day
            rows.append(row)
        return rows


def _preview_args(args: Sequence[Any], kwargs: Dict[str, Any]) -> str:
    parts = []
    for a in args[:2]:
        if isinstance(a, (list, tuple, set)):
            parts.append(f"list(n={len(a)})")
        else:
            s = str(a)
            parts.append(s if len(s) <= 50 else s[:47] + "...")
    for k in ("security", "end_date", "count", "frequency", "fq", "date", "unit"):
        if k in kwargs:
            v = kwargs[k]
            if isinstance(v, (list, tuple, set)):
                parts.append(f"{k}=list(n={len(v)})")
            else:
                parts.append(f"{k}={v}")
    return "; ".join(parts)[:240]


def _stats_row(api: str, xs: List[float], errors: int = 0) -> Dict[str, Any]:
    xs_sorted = sorted(xs)
    n = len(xs_sorted)

    def pct(p: float) -> float:
        if not n:
            return 0.0
        return xs_sorted[min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))]

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
        "share_of_api_total_pct": None,
    }


def _fill_share(rows: List[Dict[str, Any]]) -> None:
    total = sum(float(r["total_s"]) for r in rows) or 1.0
    for r in rows:
        r["share_of_api_total_pct"] = round(100.0 * float(r["total_s"]) / total, 2)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if MAIN_ENV.exists():
        load_dotenv(str(MAIN_ENV), override=False)


def _jq_auth():
    import jqdatasdk as jq

    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        raise RuntimeError("缺少 JQDATA 账号")
    jq.auth(user, pwd)
    return jq


def _mean(values: List[float]) -> float:
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else 0.0


def run_jq_native_profile() -> Dict[str, Any]:
    """对齐打板日频主路径：建宇宙 + 观察池 + 开盘扫描取数（不含完整撮合）。"""
    print("\n========== [jq_native] 打板策略数据层 ==========")
    _load_dotenv()
    jq = _jq_auth()
    import pandas as pd

    prof = CallProfiler()
    get_trade_days = prof.wrap("jq.get_trade_days", jq.get_trade_days)
    get_all_securities = prof.wrap("jq.get_all_securities", jq.get_all_securities)
    get_index_stocks = prof.wrap("jq.get_index_stocks", jq.get_index_stocks)
    get_price = prof.wrap("jq.get_price", jq.get_price)
    # attribute_history ≈ get_price count/unit
    get_bars = prof.wrap("jq.get_bars", getattr(jq, "get_bars", jq.get_price))

    stage_times: Dict[str, float] = defaultdict(float)
    counters: Dict[str, int] = {
        "universe_checks": 0,
        "universe_kept": 0,
        "watchlist_size_sum": 0,
        "scan_history_calls": 0,
    }

    wall0 = time.perf_counter()
    t = time.perf_counter()
    trade_days = list(get_trade_days(start_date=BT_START, end_date=BT_END))
    stage_times["01_load_trade_days"] += time.perf_counter() - t
    print(f"  交易日: {len(trade_days)}  universe_cap={MAX_UNIVERSE}")

    for i, day in enumerate(trade_days):
        day_d = pd.to_datetime(day).date()
        prev_d = pd.to_datetime(trade_days[max(0, i - 1)]).date()
        prof.set_day(day_d)
        day_t0 = time.perf_counter()

        # --- _resolve_universe ---
        prof.set_phase("resolve_universe_all_securities")
        t = time.perf_counter()
        try:
            stock_df = get_all_securities(types=["stock"], date=prev_d)
        except Exception:
            stock_df = pd.DataFrame()
        stage_times["02_get_all_securities"] += time.perf_counter() - t

        symbols: List[str] = []
        if stock_df is not None and not getattr(stock_df, "empty", True):
            symbols = list(stock_df.index.tolist())
        if not symbols:
            prof.set_phase("resolve_universe_index_stocks")
            t = time.perf_counter()
            symbols = list(get_index_stocks(BENCHMARK, date=prev_d) or [])
            stage_times["02b_get_index_stocks"] += time.perf_counter() - t

        prof.set_phase("resolve_universe_current_data")
        t = time.perf_counter()
        universe: List[str] = []
        end_dt = datetime.combine(day_d, datetime.min.time().replace(hour=9, minute=0))
        for symbol in symbols:
            if len(universe) >= MAX_UNIVERSE:
                break
            if not isinstance(symbol, str):
                continue
            if symbol.startswith("688") or symbol.startswith("8"):
                continue
            name = ""
            if stock_df is not None and not getattr(stock_df, "empty", True) and symbol in stock_df.index:
                row = stock_df.loc[symbol]
                name = str(row.get("display_name", "") or row.get("name", "") or "")
            if "ST" in name or "*" in name or "退" in name:
                continue
            counters["universe_checks"] += 1
            try:
                pdf = get_price(
                    symbol,
                    end_date=end_dt,
                    frequency="daily",
                    fields=["close", "paused"],
                    count=1,
                    fq="pre",
                )
                paused = False
                if pdf is not None and not pdf.empty and "paused" in pdf.columns:
                    paused = bool(pdf.iloc[-1].get("paused", False))
                if paused:
                    continue
            except Exception:
                continue
            universe.append(symbol)
        stage_times["03_universe_filter_get_price"] += time.perf_counter() - t
        counters["universe_kept"] += len(universe)

        # --- _build_daily_watchlist ---
        prof.set_phase("build_watchlist_get_price_batch")
        t = time.perf_counter()
        watchlist = {}
        if universe:
            panel = get_price(
                universe,
                end_date=prev_d,
                frequency="daily",
                fields=["close", "high", "money"],
                count=25,
                skip_paused=True,
                fq="pre",
                panel=False,
            )
            stage_times["04_watchlist_batch_get_price"] += time.perf_counter() - t
            if panel is not None and not panel.empty:
                ranked = []
                for symbol in universe:
                    stock_df2 = panel[panel["code"] == symbol].sort_index() if "code" in panel.columns else panel
                    if "code" in panel.columns:
                        stock_df2 = panel[panel["code"] == symbol].sort_index()
                    else:
                        continue
                    if len(stock_df2) < 22:
                        continue
                    closes = stock_df2["close"].tolist()
                    highs = stock_df2["high"].tolist()
                    amounts = stock_df2["money"].tolist()
                    prev_close = float(closes[-1])
                    avg_amount5 = _mean(amounts[-5:])
                    high20 = max(highs[-20:])
                    breakout_ratio = prev_close / max(high20, 0.01)
                    recent_return5 = prev_close / max(float(closes[-6]), 0.01) - 1.0
                    if prev_close < 4 or prev_close > 80:
                        continue
                    if avg_amount5 < 80_000_000:
                        continue
                    if breakout_ratio < 0.95:
                        continue
                    if recent_return5 < 0.01 or recent_return5 > 0.18:
                        continue
                    ranked.append((avg_amount5, symbol, {"prev_close": prev_close}))
                ranked.sort(reverse=True)
                watchlist = {s: info for _, s, info in ranked[:MAX_WATCHLIST]}
        else:
            stage_times["04_watchlist_batch_get_price"] += time.perf_counter() - t
        counters["watchlist_size_sum"] += len(watchlist)

        # --- 开盘扫描：对观察池前若干只做分钟历史（对齐 attribute_history）---
        prof.set_phase("scan_attribute_history")
        t = time.perf_counter()
        scan_n = 0
        open_dt = datetime.combine(day_d, datetime.min.time().replace(hour=9, minute=30))
        for symbol in list(watchlist.keys())[:20]:
            try:
                # jq.get_bars(security, count, unit='1m', ...) 若不可用则 get_price minute
                try:
                    get_bars(symbol, 20, unit="1m", end_dt=open_dt, fq_ref_date=prev_d)
                except TypeError:
                    get_price(
                        symbol,
                        end_date=open_dt,
                        frequency="minute",
                        fields=["close", "high", "low", "money", "volume"],
                        count=20,
                        fq="pre",
                    )
                except Exception:
                    get_price(
                        symbol,
                        end_date=open_dt,
                        frequency="minute",
                        fields=["close", "high", "low", "money", "volume"],
                        count=20,
                        fq="pre",
                    )
                scan_n += 1
                counters["scan_history_calls"] += 1
            except Exception:
                continue
        stage_times["05_scan_minute_history"] += time.perf_counter() - t

        # 日终：若有持仓则估值——剖析默认无持仓，跳过
        stage_times["07_per_day_wall"] += time.perf_counter() - day_t0
        print(
            f"  day {i+1}/{len(trade_days)} {day_d} "
            f"universe={len(universe)} watchlist={len(watchlist)} scan={scan_n}"
        )

    wall = time.perf_counter() - wall0
    stage_times["00_total_wall"] = wall
    return {
        "env": "jq_native",
        "note": (
            f"数据层对齐打板日频：get_all_securities → 逐票 get_price 过滤(≤{MAX_UNIVERSE}) → "
            f"批量 get_price(count=25) 建观察池(≤{MAX_WATCHLIST}) → 开盘对观察池前20只拉分钟历史；"
            "不含完整撮合。规模相对原版 2000 宇宙已收紧。"
        ),
        "start": BT_START,
        "end": BT_END,
        "config": {"max_universe_size": MAX_UNIVERSE, "max_watchlist_size": MAX_WATCHLIST},
        "trade_days": len(trade_days),
        "wall_seconds": round(wall, 6),
        "stages": dict(stage_times),
        "counters": counters,
        "api_agg": prof.aggregate(),
        "api_by_day": prof.by_day(),
        "calls": prof.calls,
        "session_manifest": None,
        "result_metrics": None,
    }


def run_bt_optimized_profile() -> Dict[str, Any]:
    print("\n========== [bt_optimized] 打板策略完整回测 ==========")
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

    for name in (
        "get_price",
        "get_current_data",
        "get_all_securities",
        "get_index_stocks",
        "get_trade_days",
        "attribute_history",
        "get_security_info",
    ):
        if hasattr(api_mod, name):
            setattr(api_mod, name, prof.wrap(f"bt.{name}", getattr(api_mod, name)))

    provider = api_mod.get_data_provider()
    for name in ("get_price", "get_all_securities", "get_index_stocks"):
        if hasattr(provider, name):
            setattr(provider, name, prof.wrap(f"provider.{name}", getattr(provider, name)))

    _orig_run_day = BacktestEngine._run_trading_day
    _orig_process = BacktestEngine._process_orders
    _orig_update_pos = BacktestEngine._update_positions
    _orig_resolve = BacktestEngine._resolve_base_exec_price
    _orig_record = BacktestEngine._record_daily

    def _timed(stage_key: str, api_name: str, orig, after_call=None):  # type: ignore[no-untyped-def]
        def inner(self, *args, **kwargs):
            prof.set_phase(stage_key)
            if args:
                try:
                    prof.set_day(args[0])
                except Exception:
                    pass
            t0 = time.perf_counter()
            try:
                return orig(self, *args, **kwargs)
            finally:
                dt = time.perf_counter() - t0
                stage_times[stage_key] += dt
                prof.calls.append(
                    {
                        "api": api_name,
                        "seconds": round(dt, 6),
                        "day": prof._day,
                        "phase": stage_key,
                        "ok": True,
                        "error": None,
                        "args_preview": "",
                        "ts": datetime.now().isoformat(timespec="milliseconds"),
                    }
                )

        return inner

    BacktestEngine._run_trading_day = _timed(  # type: ignore[method-assign]
        "engine._run_trading_day", "engine._run_trading_day", _orig_run_day
    )
    BacktestEngine._process_orders = _timed(  # type: ignore[method-assign]
        "engine._process_orders", "engine._process_orders", _orig_process
    )
    BacktestEngine._update_positions = _timed(  # type: ignore[method-assign]
        "engine._update_positions", "engine._update_positions", _orig_update_pos
    )
    BacktestEngine._record_daily = _timed(  # type: ignore[method-assign]
        "engine._record_daily", "engine._record_daily", _orig_record
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

    BacktestEngine._resolve_base_exec_price = _resolve  # type: ignore[method-assign]
    engine_mod.api_get_price = prof.wrap("engine.api_get_price", engine_mod.api_get_price)

    out = OUT_DIR / "bt_optimized_run"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    wall0 = time.perf_counter()
    results = create_backtest(
        strategy_file=str(STRATEGY),
        start_date=BT_START,
        end_date=BT_END,
        frequency="day",
        initial_cash=1_000_000,
        benchmark=BENCHMARK,
        log_file=str(out / "backtest.log"),
        data_session_config={
            "enabled": True,
            "price_block_cache_enabled": True,
            "manifest_path": str(out / "session_manifest.json"),
        },
    )
    wall = time.perf_counter() - wall0
    stage_times["00_total_wall"] = wall

    runtime = None
    manifest = None
    metrics = None
    if isinstance(results, dict):
        runtime = (results.get("meta") or {}).get("runtime_seconds")
        manifest = results.get("backtest_data_session")
        metrics = {}
        for src in (results.get("metrics") or {}, results.get("meta") or {}):
            for k in ("total_returns", "annual_returns", "max_drawdown", "sharpe", "runtime_seconds"):
                if k in src:
                    metrics[k] = src[k]

    BacktestEngine._run_trading_day = _orig_run_day  # type: ignore[method-assign]
    BacktestEngine._process_orders = _orig_process  # type: ignore[method-assign]
    BacktestEngine._update_positions = _orig_update_pos  # type: ignore[method-assign]
    BacktestEngine._resolve_base_exec_price = _orig_resolve  # type: ignore[method-assign]
    BacktestEngine._record_daily = _orig_record  # type: ignore[method-assign]

    print(f"  wall={wall:.3f}s runtime_meta={runtime}")
    return {
        "env": "bt_optimized",
        "note": (
            f"完整日频回测包装策略；宇宙≤{MAX_UNIVERSE}；会话块开启；"
            "entry_start=09:30 以便开盘 handle_data 进入扫描"
        ),
        "start": BT_START,
        "end": BT_END,
        "config": {"max_universe_size": MAX_UNIVERSE, "max_watchlist_size": MAX_WATCHLIST},
        "wall_seconds": round(wall, 6),
        "runtime_seconds_meta": runtime,
        "stages": dict(stage_times),
        "counters": None,
        "api_agg": prof.aggregate(),
        "api_by_day": prof.by_day(),
        "calls": prof.calls,
        "session_manifest": manifest,
        "result_metrics": metrics,
    }


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
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
    return [
        {
            "env": env,
            "stage": name,
            "seconds": round(float(sec), 6),
            "share_of_wall_pct": round(100.0 * float(sec) / total, 2),
        }
        for name, sec in sorted(stages.items(), key=lambda kv: -float(kv[1]))
    ]


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
    summary = {k: v for k, v in payload.items() if k not in ("calls", "api_by_day")}
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
    print("打板策略耗时剖析报告")
    print(f"区间: {BT_START} .. {BT_END}  universe≤{MAX_UNIVERSE} watchlist≤{MAX_WATCHLIST}")
    print("=" * 100)
    for p in payloads:
        print(f"\n### 环境: {p['env']}")
        print(f"说明: {p.get('note')}")
        print(f"总墙钟: {p.get('wall_seconds')} s", end="")
        if p.get("runtime_seconds_meta") is not None:
            print(f"  | meta.runtime_seconds: {p.get('runtime_seconds_meta')}", end="")
        print()
        if p.get("counters"):
            print(f"计数: {p.get('counters')}")
        print("\n[阶段耗时]")
        print(f"{'stage':<42} {'seconds':>10} {'share%':>8}")
        for r in _stages_to_rows(p["env"], p.get("stages") or {})[:25]:
            print(f"{r['stage']:<42} {r['seconds']:>10.4f} {r['share_of_wall_pct']:>7.2f}%")
        print("\n[API 汇总]")
        print(
            f"{'api':<36} {'calls':>7} {'total_s':>10} {'mean_s':>10} "
            f"{'p50_s':>10} {'p95_s':>10} {'max_s':>10} {'share%':>7}"
        )
        for r in (p.get("api_agg") or [])[:30]:
            print(
                f"{r['api']:<36} {r['calls']:>7} {r['total_s']:>10.4f} {r['mean_s']:>10.4f} "
                f"{r['p50_s']:>10.4f} {r['p95_s']:>10.4f} {r['max_s']:>10.4f} "
                f"{r.get('share_of_api_total_pct') or 0:>6.1f}%"
            )
        man = p.get("session_manifest")
        if isinstance(man, dict):
            stats = man.get("stats") or {}
            print("\n[会话]")
            print(
                f"  cache_hits={stats.get('cache_hits')} misses={stats.get('cache_misses')} "
                f"writes={stats.get('cache_writes')} "
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
    env["PYTHONPATH"] = str(SANDBOX_ROOT)
    cache_dir = OUT_DIR / f"cache_{mode}"
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["DATA_CACHE_DIR"] = str(cache_dir)
    if mode == "bt":
        env["BT_BACKTEST_DATA_SESSION"] = "true"
        env["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
    cmd = [
        str(PYTHON if PYTHON.exists() else sys.executable),
        str(SANDBOX_ROOT / "my_strategies" / "bench_daban_profile.py"),
        "--mode",
        mode,
        "--write-partial",
        str(partial),
    ]
    print(f"\n>> spawn {mode}")
    proc = subprocess.run(cmd, cwd=str(SANDBOX_ROOT), env=env, check=False, timeout=2400)
    if proc.returncode != 0:
        print(f"!! {mode} exit={proc.returncode}")
    if not partial.exists():
        return {
            "env": mode,
            "wall_seconds": 0,
            "stages": {},
            "api_agg": [],
            "calls": [],
            "note": f"failed rc={proc.returncode}",
            "error": True,
        }
    return json.loads(partial.read_text(encoding="utf-8"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["jq", "bt", "all"], default="all")
    parser.add_argument("--write-partial", type=str, default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode == "all":
        payloads = [_spawn("jq"), _spawn("bt")]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for p in payloads:
            persist_env_result(p, stamp)
        combo = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy": "打板策略",
            "start": BT_START,
            "end": BT_END,
            "config": {"max_universe_size": MAX_UNIVERSE, "max_watchlist_size": MAX_WATCHLIST},
            "envs": [
                {
                    k: p.get(k)
                    for k in (
                        "env",
                        "wall_seconds",
                        "runtime_seconds_meta",
                        "note",
                        "stages",
                        "counters",
                        "api_agg",
                        "session_manifest",
                        "result_metrics",
                        "config",
                    )
                }
                for p in payloads
            ],
        }
        text = json.dumps(combo, ensure_ascii=False, indent=2, default=str)
        (OUT_DIR / "summary_latest.json").write_text(text, encoding="utf-8")
        (OUT_DIR / stamp / "summary_all.json").write_text(text, encoding="utf-8")
        comp_api = [{"env": p.get("env"), **r} for p in payloads for r in (p.get("api_agg") or [])]
        comp_st = []
        for p in payloads:
            comp_st.extend(_stages_to_rows(p.get("env") or "", p.get("stages") or {}))
        _write_csv(OUT_DIR / "compare_api_aggregate_latest.csv", comp_api)
        _write_csv(OUT_DIR / "compare_stages_latest.csv", comp_st)
        _write_csv(OUT_DIR / stamp / "compare_api_aggregate.csv", comp_api)
        _write_csv(OUT_DIR / stamp / "compare_stages.csv", comp_st)
        print_report(payloads)
        print(f"\n结果目录: {OUT_DIR / stamp}")
        return 0

    try:
        payload = run_jq_native_profile() if args.mode == "jq" else run_bt_optimized_profile()
    except Exception:
        traceback.print_exc()
        payload = {
            "env": "jq_native" if args.mode == "jq" else "bt_optimized",
            "wall_seconds": 0,
            "stages": {},
            "api_agg": [],
            "calls": [],
            "note": "failed",
            "error": traceback.format_exc(),
        }
    if args.write_partial:
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
