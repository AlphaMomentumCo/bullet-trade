# -*- coding: utf-8 -*-
"""
单股票均值策略：聚宽原生 vs 优化后 bullet-trade 分环节精细耗时剖析。

用法（沙箱根目录）:
  C:\\Users\\90405\\anaconda3\\envs\\quant\\python.exe my_strategies\\bench_ma5_single_profile.py --mode all
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
OUT_DIR = SANDBOX_ROOT / "my_results" / "ma5_single_profile"
PYTHON = Path(r"C:\Users\90405\anaconda3\envs\quant\python.exe")
STRATEGY = SANDBOX_ROOT / "my_strategies" / "单股票均值策略.py"

BT_START = "2026-04-01"
BT_END = "2026-04-30"
SECURITY = "000001.XSHE"


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
        err_count: Dict[str, int] = defaultdict(int)
        for c in self.calls:
            buckets[c["api"]].append(float(c["seconds"]))
            if not c.get("ok", True):
                err_count[c["api"]] += 1
        rows = []
        for api, xs in sorted(buckets.items(), key=lambda kv: -sum(kv[1])):
            rows.append(_stats_row(api, xs, errors=err_count.get(api, 0)))
        _fill_share(rows)
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


def _preview_args(args: Sequence[Any], kwargs: Dict[str, Any]) -> str:
    parts = []
    for a in args[:2]:
        s = str(a)
        parts.append(s if len(s) <= 60 else s[:57] + "...")
    for k in ("security", "start_date", "end_date", "count", "frequency", "fq", "date"):
        if k in kwargs:
            parts.append(f"{k}={kwargs[k]}")
    return "; ".join(parts)[:240]


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
        "share_of_api_total_pct": None,
    }


def _fill_share(api_rows: List[Dict[str, Any]]) -> None:
    total = sum(float(r["total_s"]) for r in api_rows) or 1.0
    for r in api_rows:
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


def run_jq_native_profile() -> Dict[str, Any]:
    print("\n========== [jq_native] 单股票均值 — 聚宽原生数据层 ==========")
    _load_dotenv()
    jq = _jq_auth()
    prof = CallProfiler()
    get_trade_days = prof.wrap("jq.get_trade_days", jq.get_trade_days)
    get_price = prof.wrap("jq.get_price", jq.get_price)

    import pandas as pd

    stage_times: Dict[str, float] = defaultdict(float)
    counters: Dict[str, int] = {
        "signal_days": 0,
        "buy_signals": 0,
        "sell_signals": 0,
        "skip_insufficient": 0,
    }

    wall0 = time.perf_counter()
    t = time.perf_counter()
    trade_days = list(get_trade_days(start_date=BT_START, end_date=BT_END))
    stage_times["01_load_trade_days"] += time.perf_counter() - t
    print(f"  交易日数: {len(trade_days)}  security={SECURITY}")

    cash = 1_000_000.0
    position_amount = 0
    avg_cost = 0.0

    for i, day in enumerate(trade_days):
        day_d = pd.to_datetime(day).date()
        prev_idx = max(0, i - 1)
        prev_d = pd.to_datetime(trade_days[prev_idx]).date()
        prof.set_day(day_d)
        day_t0 = time.perf_counter()

        # before_market_open: 仅设标的，可忽略
        prof.set_phase("before_market_open")
        security = SECURITY

        # market_open: get_price count=6（与策略一致，end_date=previous_date）
        prof.set_phase("market_open_get_price_ma5")
        t = time.perf_counter()
        close_data = get_price(
            security,
            end_date=prev_d,
            count=6,
            frequency="daily",
            fields=["close"],
            fq="pre",
        )
        stage_times["02_market_open_get_price_ma5"] += time.perf_counter() - t
        counters["signal_days"] += 1

        if close_data is None or len(close_data) < 5:
            counters["skip_insufficient"] += 1
            stage_times["07_per_day_wall"] += time.perf_counter() - day_t0
            continue

        MA5 = float(close_data["close"].tail(5).mean())
        current_price = float(close_data["close"].iloc[-1])

        # 模拟买卖时的撮合取价（开盘价点查）
        open_dt = datetime.combine(day_d, datetime.min.time().replace(hour=9, minute=30))
        if (current_price < 1.01 * MA5) and cash > 0:
            counters["buy_signals"] += 1
            prof.set_phase("rebalance_buy_exec_price")
            t = time.perf_counter()
            pdf = get_price(
                security,
                end_date=open_dt,
                frequency="daily",
                fields=["open", "close"],
                count=1,
                fq="pre",
            )
            stage_times["03_buy_exec_get_price"] += time.perf_counter() - t
            px = float(pdf.iloc[-1]["open"]) if pdf is not None and not pdf.empty else current_price
            if px > 0 and cash > 0:
                amt = int(cash / px / 100) * 100
                if amt > 0:
                    cost = amt * px
                    fee = max(5.0, cost * 0.0003)
                    cash -= cost + fee
                    position_amount = amt
                    avg_cost = px
        elif current_price > MA5 and position_amount > 0:
            counters["sell_signals"] += 1
            prof.set_phase("rebalance_sell_exec_price")
            t = time.perf_counter()
            pdf = get_price(
                security,
                end_date=open_dt,
                frequency="daily",
                fields=["open", "close"],
                count=1,
                fq="pre",
            )
            stage_times["04_sell_exec_get_price"] += time.perf_counter() - t
            px = float(pdf.iloc[-1]["open"]) if pdf is not None and not pdf.empty else current_price
            if px > 0 and position_amount > 0:
                proceeds = position_amount * px
                fee = max(5.0, proceeds * 0.0003) + proceeds * 0.001
                cash += proceeds - fee
                position_amount = 0
                avg_cost = 0.0

        # 日终估值
        if position_amount > 0:
            prof.set_phase("day_end_mark")
            close_dt = datetime.combine(day_d, datetime.min.time().replace(hour=15, minute=0))
            t = time.perf_counter()
            get_price(
                security,
                end_date=close_dt,
                frequency="daily",
                fields=["close"],
                count=1,
                fq="none",
            )
            stage_times["05_day_end_mark_get_price"] += time.perf_counter() - t

        # after_market_close: get_trades 在原生侧无对应，记 0
        stage_times["07_per_day_wall"] += time.perf_counter() - day_t0
        if (i + 1) % 5 == 0 or i == 0 or i == len(trade_days) - 1:
            print(
                f"  day {i+1}/{len(trade_days)} {day_d} "
                f"pos={position_amount} cash={cash:.0f} MA5={MA5:.2f} px={current_price:.2f}"
            )

    wall = time.perf_counter() - wall0
    stage_times["00_total_wall"] = wall
    return {
        "env": "jq_native",
        "note": (
            "数据层日程模拟：每日 market_open 的 get_price(count=6) + "
            "买卖信号时 count=1 执行价 + 持仓日终估值；单标的 000001.XSHE"
        ),
        "start": BT_START,
        "end": BT_END,
        "security": SECURITY,
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
    print("\n========== [bt_optimized] 单股票均值 — 优化后完整回测 ==========")
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

    api_mod.get_price = prof.wrap("bt.get_price", api_mod.get_price)
    api_mod.get_current_data = prof.wrap("bt.get_current_data", api_mod.get_current_data)
    api_mod.get_trade_days = prof.wrap("bt.get_trade_days", api_mod.get_trade_days)
    if hasattr(api_mod, "get_trades"):
        api_mod.get_trades = prof.wrap("bt.get_trades", api_mod.get_trades)
    if hasattr(api_mod, "get_security_info"):
        api_mod.get_security_info = prof.wrap("bt.get_security_info", api_mod.get_security_info)

    provider = api_mod.get_data_provider()
    if hasattr(provider, "get_price"):
        provider.get_price = prof.wrap("provider.get_price", provider.get_price)

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
        "note": "完整回测 + API/引擎猴子补丁；会话行情块开启；独立磁盘缓存；单标的 MA5",
        "start": BT_START,
        "end": BT_END,
        "security": SECURITY,
        "trade_days": None,
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
    print("单股票均值策略耗时剖析报告")
    print(f"区间: {BT_START} .. {BT_END}  标的: {SECURITY}")
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
        for r in _stages_to_rows(p["env"], p.get("stages") or {})[:20]:
            print(f"{r['stage']:<42} {r['seconds']:>10.4f} {r['share_of_wall_pct']:>7.2f}%")
        print("\n[API 汇总]")
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
    cache_dir = OUT_DIR / (f"cache_{'bt' if mode == 'bt' else 'jq'}")
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["DATA_CACHE_DIR"] = str(cache_dir)
    if mode == "bt":
        env["BT_BACKTEST_DATA_SESSION"] = "true"
        env["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"

    cmd = [
        str(PYTHON if PYTHON.exists() else sys.executable),
        str(SANDBOX_ROOT / "my_strategies" / "bench_ma5_single_profile.py"),
        "--mode",
        mode,
        "--write-partial",
        str(partial),
    ]
    print(f"\n>> spawn {mode}")
    proc = subprocess.run(cmd, cwd=str(SANDBOX_ROOT), env=env, check=False, timeout=900)
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
    parser.add_argument("--mode", choices=["jq", "bt", "all"], default="all")
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
        combo = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy": "单股票均值策略",
            "security": SECURITY,
            "start": BT_START,
            "end": BT_END,
            "envs": [
                {
                    "env": p.get("env"),
                    "wall_seconds": p.get("wall_seconds"),
                    "runtime_seconds_meta": p.get("runtime_seconds_meta"),
                    "note": p.get("note"),
                    "stages": p.get("stages"),
                    "counters": p.get("counters"),
                    "api_agg": p.get("api_agg"),
                    "session_manifest": p.get("session_manifest"),
                    "result_metrics": p.get("result_metrics"),
                }
                for p in payloads
            ],
            "paths": all_paths,
        }
        text = json.dumps(combo, ensure_ascii=False, indent=2, default=str)
        (OUT_DIR / "summary_latest.json").write_text(text, encoding="utf-8")
        (OUT_DIR / stamp / "summary_all.json").write_text(text, encoding="utf-8")
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
        print(f"汇总: {OUT_DIR / 'summary_latest.json'}")
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
            "api_by_day": [],
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
