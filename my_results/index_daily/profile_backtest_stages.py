"""
按 mini_demo 逻辑，分别对 hs300 / zz1000 股票池跑现有回测框架，统计各环节耗时。

用法（quant 环境）:
  python my_results/index_daily/profile_backtest_stages.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

START = os.getenv("BT_PROFILE_START", "2025-05-01")
END = os.getenv("BT_PROFILE_END", "2025-06-30")
CASH = float(os.getenv("BT_PROFILE_CASH", "100000"))
STRATEGY = ROOT / "my_strategies" / "mini_demo_index_universe.py"
OUT_DIR = ROOT / "my_results" / "index_daily" / "profile_backtest"
UNIVERSES = ["hs300", "zz1000"]

_CURRENT_TIMER: Optional["StageTimer"] = None
_PROBES_INSTALLED = False


class StageTimer:
    def __init__(self) -> None:
        self.total: Dict[str, float] = defaultdict(float)
        self.count: Dict[str, int] = defaultdict(int)

    @contextmanager
    def track(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            self.total[name] += dt
            self.count[name] += 1

    def summary(self) -> List[Dict[str, Any]]:
        wall = self.total.get("wall_total", 0.0) or 1e-12
        rows = []
        for name, sec in sorted(self.total.items(), key=lambda x: -x[1]):
            rows.append(
                {
                    "stage": name,
                    "seconds": round(sec, 4),
                    "calls": self.count[name],
                    "avg_ms": round(sec / self.count[name] * 1000, 3) if self.count[name] else 0.0,
                    "pct_of_wall": round(sec / wall * 100, 2),
                }
            )
        return rows


def _bind(stage: str):
    def decorator(fn):
        def wrapped(*args, **kwargs):
            timer = _CURRENT_TIMER
            if timer is None:
                return fn(*args, **kwargs)
            with timer.track(stage):
                return fn(*args, **kwargs)

        return wrapped

    return decorator


def install_probes_once() -> None:
    global _PROBES_INSTALLED
    if _PROBES_INSTALLED:
        return

    from bullet_trade.core import engine as engine_mod
    from bullet_trade.data import api as data_api
    from bullet_trade.data.providers import jqdata as jq_provider_mod

    Engine = engine_mod.BacktestEngine
    for attr, stage in [
        ("load_strategy", "engine.load_strategy"),
        ("_run_trading_day", "engine._run_trading_day"),
        ("_process_orders", "engine._process_orders"),
        ("_resolve_base_exec_price", "engine._resolve_base_exec_price"),
        ("_update_positions", "engine._update_positions"),
        ("_record_daily", "engine._record_daily"),
        ("_record_daily_positions", "engine._record_daily_positions"),
        ("_load_benchmark_data", "engine._load_benchmark_data"),
        ("_apply_dividends_for_day", "engine._apply_dividends_for_day"),
        ("_generate_results", "engine._generate_results"),
    ]:
        setattr(Engine, attr, _bind(stage)(getattr(Engine, attr)))

    for attr, stage in [
        ("get_price", "api.get_price"),
        ("get_fundamentals", "api.get_fundamentals"),
        ("get_current_data", "api.get_current_data"),
        ("get_security_info", "api.get_security_info"),
        ("get_trade_days", "api.get_trade_days"),
        ("get_split_dividend", "api.get_split_dividend"),
    ]:
        setattr(data_api, attr, _bind(stage)(getattr(data_api, attr)))

    # 引擎模块内已绑定的符号也要替换，否则撮合/日终走不到探针
    engine_mod._data_api_get_price = data_api.get_price  # type: ignore[attr-defined]
    engine_mod.get_security_info = data_api.get_security_info  # type: ignore[attr-defined]

    BCD = data_api.BacktestCurrentData
    BCD.__getitem__ = _bind("api.current_data.__getitem__")(BCD.__getitem__)  # type: ignore[method-assign]

    Provider = jq_provider_mod.JQDataProvider
    for attr, stage in [
        ("get_price", "provider.jq.get_price"),
        ("get_fundamentals", "provider.jq.get_fundamentals"),
        ("get_trade_days", "provider.jq.get_trade_days"),
        ("get_split_dividend", "provider.jq.get_split_dividend"),
    ]:
        setattr(Provider, attr, _bind(stage)(getattr(Provider, attr)))

    _PROBES_INSTALLED = True


def run_one(universe: str) -> Dict[str, Any]:
    global _CURRENT_TIMER
    os.environ["BT_UNIVERSE"] = universe
    os.environ.setdefault("MESSAGE_CHANNEL", "")

    from bullet_trade.core.engine import create_backtest
    from bullet_trade.core.globals import log

    try:
        log.set_level("system", "warning")
        log.set_level("strategy", "warning")
    except Exception:
        pass

    install_probes_once()
    timer = StageTimer()
    _CURRENT_TIMER = timer

    t0 = time.perf_counter()
    with timer.track("wall_total"):
        results = create_backtest(
            strategy_file=str(STRATEGY),
            start_date=START,
            end_date=END,
            frequency="day",
            initial_cash=CASH,
            benchmark="000300.XSHG",
            log_file=str(OUT_DIR / f"backtest_{universe}.log"),
        )
    wall = time.perf_counter() - t0
    _CURRENT_TIMER = None

    metrics = results.get("metrics") or {}
    return {
        "universe": universe,
        "start": START,
        "end": END,
        "wall_seconds": round(wall, 4),
        "metrics_total_return": metrics.get("total_return"),
        "stages": timer.summary(),
    }


def _write_report(report: Dict[str, Any]) -> None:
    import pandas as pd

    universe = report["universe"]
    (OUT_DIR / f"stages_{universe}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pd.DataFrame(report["stages"]).to_csv(
        OUT_DIR / f"stages_{universe}.csv", index=False, encoding="utf-8-sig"
    )


def _write_compare_csv(reports: List[Dict[str, Any]]) -> None:
    import pandas as pd

    rows = []
    for report in reports:
        for stage in report["stages"]:
            rows.append(
                {
                    "universe": report["universe"],
                    "wall_seconds": report["wall_seconds"],
                    **stage,
                }
            )
    pd.DataFrame(rows).to_csv(OUT_DIR / "stages_compare.csv", index=False, encoding="utf-8-sig")


def _print_top(report: Dict[str, Any], n: int = 18) -> None:
    print(f"\n[{report['universe']}] top stages:")
    for row in report["stages"][:n]:
        print(
            f"  {row['stage']:40s} {row['seconds']:8.3f}s  "
            f"calls={row['calls']:<6d} avg={row['avg_ms']:.2f}ms  {row['pct_of_wall']}%"
        )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reports = []
    for universe in UNIVERSES:
        print("=" * 60)
        print(f"profiling universe={universe} {START} -> {END}")
        report = run_one(universe)
        reports.append(report)
        _write_report(report)
        print(f"done {universe}: wall={report['wall_seconds']}s")
        _print_top(report)

    (OUT_DIR / "compare_hs300_zz1000.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_compare_csv(reports)
    print(f"\nreports -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
