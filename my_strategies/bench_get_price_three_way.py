# -*- coding: utf-8 -*-
"""
Compare get_price / short-backtest latency across three environments:
  A) jqdata_native  — jqdatasdk directly
  B) framework_before — bullet_trade from MAIN repo (no session)
  C) framework_after  — bullet_trade from THIS sandbox (session + price blocks)

Usage:
  python my_strategies/bench_get_price_three_way.py --mode all
  python my_strategies/bench_get_price_three_way.py --mode jqdata|before|after
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

SANDBOX_ROOT = Path(r"C:\Users\90405\Documents\bullet-trade-sandbox-gp")
MAIN_ROOT = Path(r"C:\Users\90405\Documents\bullet-trade")
MAIN_ENV = MAIN_ROOT / ".env"
OUT_DIR = SANDBOX_ROOT / "my_results" / "get_price_three_way"
PYTHON = Path(r"C:\Users\90405\anaconda3\envs\quant\python.exe")

FIXED_SECURITIES = [
    "600519.XSHG",
    "000858.XSHE",
    "601318.XSHG",
    "000001.XSHE",
    "600036.XSHG",
    "000333.XSHE",
    "601166.XSHG",
    "600276.XSHG",
    "002415.XSHE",
    "300750.XSHE",
    "000651.XSHE",
    "600030.XSHG",
    "601888.XSHG",
    "002594.XSHE",
    "300059.XSHE",
    "600900.XSHG",
    "601012.XSHG",
    "000725.XSHE",
    "601398.XSHG",
    "600887.XSHG",
    "002304.XSHE",
    "600309.XSHG",
    "000568.XSHE",
    "601288.XSHG",
    "600048.XSHG",
    "002142.XSHE",
    "600104.XSHG",
    "000063.XSHE",
    "601668.XSHG",
    "300015.XSHE",
]

# JQ 试用账号权限窗口约 2025-05-01 ~ 2026-05-08；落在窗口内才能取数
END_DATE = "2026-05-07"
BT_DAY_START = "2026-04-27"
BT_DAY_END = "2026-04-30"
BT_MINUTE_START = "2026-05-07"
BT_MINUTE_END = "2026-05-07"
BT_STUB_START = "2026-04-28"
BT_STUB_END = "2026-04-30"
W2_N = 20
W3_SECS = 5
BACKTEST_TIMEOUT_SEC = 600


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if MAIN_ENV.exists():
        load_dotenv(str(MAIN_ENV), override=False)
    os.environ.setdefault("BT_ENV_FILE", str(MAIN_ENV))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _record(
    rows: List[Dict[str, Any]],
    *,
    env: str,
    phase: str,
    workload: str,
    seconds: float,
    notes: str = "",
    ok: bool = True,
) -> None:
    rows.append(
        {
            "env": env,
            "phase": phase,
            "workload": workload,
            "seconds": round(float(seconds), 6),
            "ok": bool(ok),
            "notes": notes,
            "ts": _now_iso(),
        }
    )
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {env}/{phase}/{workload}: {seconds:.3f}s  {notes}")


def _time_call(fn):
    t0 = time.perf_counter()
    result = fn()
    return time.perf_counter() - t0, result


def _sample_securities_jq(jq) -> List[str]:
    try:
        stocks = jq.get_index_stocks("000300.XSHG", date=END_DATE) or []
        stocks = [s for s in stocks if isinstance(s, str)]
        if len(stocks) >= 30:
            return stocks[:30]
    except Exception as exc:
        print(f"  warn: HS300 constituents unavailable ({exc}); using fixed list")
    return list(FIXED_SECURITIES)


def _sample_securities_framework() -> List[str]:
    try:
        from bullet_trade.data.api import get_data_provider

        provider = get_data_provider()
        stocks = provider.get_index_stocks("000300.XSHG", date=END_DATE) or []
        stocks = [s for s in stocks if isinstance(s, str)]
        if len(stocks) >= 30:
            return stocks[:30]
    except Exception as exc:
        print(f"  warn: framework HS300 unavailable ({exc}); using fixed list")
    return list(FIXED_SECURITIES)


def _set_fake_context(dt: datetime) -> None:
    from bullet_trade.data.api import set_current_context

    ctx = SimpleNamespace(
        current_dt=dt,
        previous_date=(dt.date() if hasattr(dt, "date") else dt),
        previous_dt=dt,
        run_params={},
    )
    set_current_context(ctx)


def run_jqdata_microbench(rows: List[Dict[str, Any]]) -> None:
    env = "jqdata_native"
    print(f"\n=== {env} microbench ===")
    import jqdatasdk as jq

    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        _record(rows, env=env, phase="cold", workload="auth", seconds=0, notes="missing JQDATA creds", ok=False)
        return
    jq.auth(user, pwd)
    securities = _sample_securities_jq(jq)
    fields_w1 = ["close", "high", "money"]
    fields_w2 = ["open", "close", "high", "low", "volume", "money"]
    end_dt = END_DATE

    def w1():
        return jq.get_price(
            securities,
            count=25,
            end_date=end_dt,
            frequency="daily",
            fields=fields_w1,
            panel=False,
            fq="pre",
        )

    def w2():
        out = []
        for sec in securities[:W2_N]:
            out.append(
                jq.get_price(
                    sec,
                    count=1,
                    end_date=end_dt,
                    frequency="daily",
                    fields=fields_w2,
                    fq="pre",
                )
            )
        return out

    def w3():
        out = []
        for sec in securities[:W3_SECS]:
            out.append(
                jq.get_price(
                    sec,
                    count=1,
                    end_date=f"{END_DATE} 14:30:00",
                    frequency="1m",
                    fields=["close", "volume"],
                    fq="pre",
                )
            )
        return out

    for phase in ("cold", "warm"):
        for name, fn, note in (
            ("W1_batch_daily25", w1, f"n={len(securities)}"),
            ("W2_point_daily1", w2, f"n={min(W2_N, len(securities))}"),
            ("W3_minute1", w3, f"n={min(W3_SECS, len(securities))}"),
        ):
            try:
                sec, _ = _time_call(fn)
                _record(rows, env=env, phase=phase, workload=name, seconds=sec, notes=note)
            except Exception as exc:
                _record(
                    rows,
                    env=env,
                    phase=phase,
                    workload=name,
                    seconds=0,
                    notes=f"{type(exc).__name__}: {exc}",
                    ok=False,
                )


def run_framework_microbench(rows: List[Dict[str, Any]], env: str) -> None:
    print(f"\n=== {env} microbench ===")
    from bullet_trade.data.api import get_price
    from bullet_trade.utils.env_loader import load_env

    load_env(str(MAIN_ENV), verbose=False, override=False)
    _set_fake_context(datetime(2026, 5, 7, 15, 0, 0))
    securities = _sample_securities_framework()
    fields_w1 = ["close", "high", "money"]
    fields_w2 = ["open", "close", "high", "low", "volume", "money"]
    end_dt = datetime(2026, 5, 7, 15, 0, 0)

    def w1():
        return get_price(
            securities,
            count=25,
            end_date=end_dt,
            frequency="daily",
            fields=fields_w1,
            panel=False,
            fq="pre",
        )

    def w2():
        out = []
        for sec in securities[:W2_N]:
            out.append(
                get_price(
                    sec,
                    count=1,
                    end_date=end_dt,
                    frequency="daily",
                    fields=fields_w2,
                    fq="pre",
                )
            )
        return out

    def w3():
        out = []
        for sec in securities[:W3_SECS]:
            out.append(
                get_price(
                    sec,
                    count=1,
                    end_date=datetime(2026, 5, 7, 14, 30, 0),
                    frequency="minute",
                    fields=["close", "volume"],
                    fq="pre",
                )
            )
        return out

    for phase in ("cold", "warm"):
        for name, fn, note in (
            ("W1_batch_daily25", w1, f"n={len(securities)}"),
            ("W2_point_daily1", w2, f"n={min(W2_N, len(securities))}"),
            ("W3_minute1", w3, f"n={min(W3_SECS, len(securities))}"),
        ):
            try:
                sec, _ = _time_call(fn)
                _record(rows, env=env, phase=phase, workload=name, seconds=sec, notes=note)
            except Exception as exc:
                _record(
                    rows,
                    env=env,
                    phase=phase,
                    workload=name,
                    seconds=0,
                    notes=f"{type(exc).__name__}: {exc}",
                    ok=False,
                )


def _run_create_backtest(
    strategy_file: Path,
    *,
    start: str,
    end: str,
    frequency: str,
    data_session_config: Optional[Dict[str, Any]],
    output_dir: Path,
) -> Dict[str, Any]:
    from bullet_trade.core.engine import create_backtest
    from bullet_trade.utils.env_loader import load_env

    load_env(str(MAIN_ENV), verbose=False, override=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(output_dir / "backtest.log")
    return create_backtest(
        strategy_file=str(strategy_file),
        start_date=start,
        end_date=end,
        frequency=frequency,
        initial_cash=1_000_000,
        benchmark="000300.XSHG",
        log_file=log_file,
        data_session_config=data_session_config,
    )


def _run_one_backtest_pair(
    rows: List[Dict[str, Any]],
    *,
    env: str,
    strat: Path,
    start: str,
    end: str,
    freq: str,
    label: str,
    session_cfg: Optional[Dict[str, Any]],
    out: Path,
    workload: str,
) -> bool:
    notes = f"{label} {start}..{end} freq={freq}"
    print(f"  trying {notes} ...")
    t0 = time.perf_counter()
    try:
        results = _run_create_backtest(
            strat,
            start=start,
            end=end,
            frequency=freq,
            data_session_config=session_cfg,
            output_dir=out / label,
        )
        elapsed = time.perf_counter() - t0
        runtime = None
        if isinstance(results, dict):
            runtime = (results.get("meta") or {}).get("runtime_seconds")
        used = float(runtime) if runtime is not None else elapsed
        _record(rows, env=env, phase="cold", workload=workload, seconds=used, notes=notes)
        t1 = time.perf_counter()
        results2 = _run_create_backtest(
            strat,
            start=start,
            end=end,
            frequency=freq,
            data_session_config=session_cfg,
            output_dir=out / f"{label}_warm",
        )
        elapsed2 = time.perf_counter() - t1
        runtime2 = None
        if isinstance(results2, dict):
            runtime2 = (results2.get("meta") or {}).get("runtime_seconds")
        used2 = float(runtime2) if runtime2 is not None else elapsed2
        _record(rows, env=env, phase="warm", workload=workload, seconds=used2, notes=notes)
        return True
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _record(
            rows,
            env=env,
            phase="cold",
            workload=workload,
            seconds=elapsed,
            notes=f"{type(exc).__name__}: {exc}",
            ok=False,
        )
        traceback.print_exc()
        return False


def run_framework_backtest(rows: List[Dict[str, Any]], env: str, *, enable_session: bool) -> None:
    print(f"\n=== {env} short backtest ===")
    strategy = SANDBOX_ROOT / "my_strategies" / "daban_demo_for_bench.py"
    stub = SANDBOX_ROOT / "my_strategies" / "bench_stub_get_price.py"
    out = OUT_DIR / env / "backtest"
    if enable_session:
        os.environ["BT_BACKTEST_DATA_SESSION"] = "true"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
        session_cfg: Optional[Dict[str, Any]] = {
            "enabled": True,
            "price_block_cache_enabled": True,
            "manifest_path": str(out / "session_manifest.json"),
        }
    else:
        os.environ["BT_BACKTEST_DATA_SESSION"] = "false"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "false"
        session_cfg = None

    # 1) 打板日频 demo（偏 watchlist 批量 get_price）
    _run_one_backtest_pair(
        rows,
        env=env,
        strat=strategy,
        start=BT_DAY_START,
        end=BT_DAY_END,
        freq="day",
        label="daban_day",
        session_cfg=session_cfg,
        out=out,
        workload="backtest_daban_day",
    )
    # 2) stub 分钟：刻意放大 current_data + count=1 双路径（优化主战场）
    ok_stub = _run_one_backtest_pair(
        rows,
        env=env,
        strat=stub,
        start=BT_MINUTE_START,
        end=BT_MINUTE_END,
        freq="minute",
        label="stub_minute_2d",
        session_cfg=session_cfg,
        out=out,
        workload="backtest_stub_minute",
    )
    if not ok_stub:
        _run_one_backtest_pair(
            rows,
            env=env,
            strat=stub,
            start=BT_STUB_START,
            end=BT_STUB_END,
            freq="day",
            label="stub_day",
            session_cfg=session_cfg,
            out=out,
            workload="backtest_stub_day",
        )


def run_framework_stub_minute_only(rows: List[Dict[str, Any]], env: str, *, enable_session: bool) -> None:
    """仅跑 stub 分钟热路径，避免被同进程 daban 预热磁盘缓存污染 cold。"""
    print(f"\n=== {env} stub_minute only ===")
    stub = SANDBOX_ROOT / "my_strategies" / "bench_stub_get_price.py"
    out = OUT_DIR / env / "stub_only"
    if enable_session:
        os.environ["BT_BACKTEST_DATA_SESSION"] = "true"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
        session_cfg: Optional[Dict[str, Any]] = {
            "enabled": True,
            "price_block_cache_enabled": True,
            "manifest_path": str(out / "session_manifest.json"),
        }
    else:
        os.environ["BT_BACKTEST_DATA_SESSION"] = "false"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "false"
        session_cfg = None
    _run_one_backtest_pair(
        rows,
        env=env,
        strat=stub,
        start=BT_MINUTE_START,
        end=BT_MINUTE_END,
        freq="minute",
        label="stub_minute_hot",
        session_cfg=session_cfg,
        out=out,
        workload="backtest_stub_minute",
    )


def run_mode(mode: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    _load_dotenv()
    if mode == "jqdata":
        run_jqdata_microbench(rows)
    elif mode == "before":
        # Ensure MAIN package wins
        main_s = str(MAIN_ROOT)
        while main_s in sys.path:
            sys.path.remove(main_s)
        sys.path.insert(0, main_s)
        # Drop sandbox from path if present ahead of main
        sand_s = str(SANDBOX_ROOT)
        if sand_s in sys.path:
            sys.path.remove(sand_s)
        os.environ["BT_BACKTEST_DATA_SESSION"] = "false"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "false"
        run_framework_microbench(rows, "framework_before")
        run_framework_backtest(rows, "framework_before", enable_session=False)
    elif mode == "after":
        sand_s = str(SANDBOX_ROOT)
        while sand_s in sys.path:
            sys.path.remove(sand_s)
        sys.path.insert(0, sand_s)
        main_s = str(MAIN_ROOT)
        if main_s in sys.path:
            sys.path.remove(main_s)
        os.environ["BT_BACKTEST_DATA_SESSION"] = "true"
        os.environ["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
        run_framework_microbench(rows, "framework_after")
        run_framework_backtest(rows, "framework_after", enable_session=True)
    elif mode == "before_stub":
        main_s = str(MAIN_ROOT)
        while main_s in sys.path:
            sys.path.remove(main_s)
        sys.path.insert(0, main_s)
        sand_s = str(SANDBOX_ROOT)
        if sand_s in sys.path:
            sys.path.remove(sand_s)
        run_framework_stub_minute_only(rows, "framework_before", enable_session=False)
    elif mode == "after_stub":
        sand_s = str(SANDBOX_ROOT)
        while sand_s in sys.path:
            sys.path.remove(sand_s)
        sys.path.insert(0, sand_s)
        main_s = str(MAIN_ROOT)
        if main_s in sys.path:
            sys.path.remove(main_s)
        run_framework_stub_minute_only(rows, "framework_after", enable_session=True)
    else:
        raise ValueError(f"unknown mode: {mode}")
    return rows


def _spawn_mode(mode: str) -> List[Dict[str, Any]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    partial = OUT_DIR / f"_partial_{mode}.json"
    if partial.exists():
        partial.unlink()
    env = os.environ.copy()
    env["BT_ENV_FILE"] = str(MAIN_ENV)
    # 隔离磁盘缓存，避免 jq→before→after 共享 ~/.bullet-trade/cache 导致 cold 失真
    cache_dir = OUT_DIR / f"cache_{mode}"
    if cache_dir.exists():
        import shutil

        shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["DATA_CACHE_DIR"] = str(cache_dir)
    if mode in ("before", "before_stub"):
        env["PYTHONPATH"] = str(MAIN_ROOT)
        env["BT_BACKTEST_DATA_SESSION"] = "false"
        env["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "false"
    elif mode in ("after", "after_stub"):
        env["PYTHONPATH"] = str(SANDBOX_ROOT)
        env["BT_BACKTEST_DATA_SESSION"] = "true"
        env["BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS"] = "true"
    else:
        env["PYTHONPATH"] = str(SANDBOX_ROOT)

    cmd = [
        str(PYTHON if PYTHON.exists() else sys.executable),
        str(SANDBOX_ROOT / "my_strategies" / "bench_get_price_three_way.py"),
        "--mode",
        mode,
        "--write-partial",
        str(partial),
    ]
    print(f"\n>> spawn {' '.join(cmd)}")
    print(f"   PYTHONPATH={env.get('PYTHONPATH')}")
    # jqdata microbench is fast; framework modes include short backtests
    timeout = 1800 if mode in ("before", "after", "before_stub", "after_stub") else 180
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SANDBOX_ROOT),
            env=env,
            check=False,
            timeout=timeout,
        )
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        print(f"!! subprocess {mode} timed out after {timeout}s")
        rc = 124
    if rc != 0:
        print(f"!! subprocess {mode} exited {rc}")
    if not partial.exists():
        return [
            {
                "env": {
                    "jqdata": "jqdata_native",
                    "before": "framework_before",
                    "after": "framework_after",
                }.get(mode, mode),
                "phase": "cold",
                "workload": "subprocess",
                "seconds": 0,
                "ok": False,
                "notes": f"subprocess exit={rc}, no partial",
                "ts": _now_iso(),
            }
        ]
    return json.loads(partial.read_text(encoding="utf-8"))


def _write_outputs(rows: Sequence[Dict[str, Any]]) -> Dict[str, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"latency_{stamp}.csv"
    json_path = OUT_DIR / f"latency_{stamp}.json"
    latest_csv = OUT_DIR / "latency_latest.csv"
    latest_json = OUT_DIR / "latency_latest.json"

    fieldnames = ["env", "phase", "workload", "seconds", "ok", "notes", "ts"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})

    payload = {
        "generated_at": _now_iso(),
        "sandbox_root": str(SANDBOX_ROOT),
        "main_root": str(MAIN_ROOT),
        "rows": list(rows),
        "summary": _summarize(rows),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_csv.write_text(csv_path.read_text(encoding="utf-8"), encoding="utf-8")
    latest_json.write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "csv": csv_path,
        "json": json_path,
        "latest_csv": latest_csv,
        "latest_json": latest_json,
    }


def _summarize(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for row in rows:
        key = f"{row.get('env')}|{row.get('phase')}|{row.get('workload')}"
        summary[key] = {
            "seconds": row.get("seconds"),
            "ok": row.get("ok"),
            "notes": row.get("notes"),
        }
    return summary


def _print_summary_table(rows: Sequence[Dict[str, Any]]) -> None:
    print("\n" + "=" * 88)
    print(f"{'env':<18} {'phase':<6} {'workload':<24} {'seconds':>10}  notes")
    print("-" * 88)
    for row in rows:
        print(
            f"{str(row.get('env')):<18} {str(row.get('phase')):<6} "
            f"{str(row.get('workload')):<24} {float(row.get('seconds') or 0):>10.3f}  "
            f"{row.get('notes')}"
        )
    print("=" * 88)

    # Compact pivot for warm W1/W2/backtest
    focus = ("W1_batch_daily25", "W2_point_daily1", "W3_minute1", "backtest_runtime")
    print("\nWarm focus:")
    for env in ("jqdata_native", "framework_before", "framework_after"):
        parts = []
        for wl in focus:
            match = next(
                (
                    r
                    for r in rows
                    if r.get("env") == env and r.get("phase") == "warm" and r.get("workload") == wl
                ),
                None,
            )
            if match is None:
                match = next(
                    (
                        r
                        for r in rows
                        if r.get("env") == env
                        and r.get("phase") == "cold"
                        and r.get("workload") == wl
                    ),
                    None,
                )
                phase = "cold"
            else:
                phase = "warm"
            if match:
                parts.append(f"{wl}={match.get('seconds'):.3f}s({phase})")
        print(f"  {env}: " + (", ".join(parts) if parts else "(no rows)"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Three-way get_price latency bench")
    parser.add_argument(
        "--mode",
        choices=["jqdata", "before", "after", "before_stub", "after_stub", "all", "stub_hot"],
        default="all",
    )
    parser.add_argument(
        "--write-partial",
        type=str,
        default=None,
        help="Write rows JSON for parent aggregator (subprocess worker)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.mode in ("all", "stub_hot"):
        all_rows: List[Dict[str, Any]] = []
        modes = ("jqdata", "before", "after") if args.mode == "all" else ("before_stub", "after_stub")
        for m in modes:
            all_rows.extend(_spawn_mode(m))
        paths = _write_outputs(all_rows)
        _print_summary_table(all_rows)
        print("\nResults:")
        for k, p in paths.items():
            print(f"  {k}: {p}")
        return 0

    rows = run_mode(args.mode)
    if args.write_partial:
        Path(args.write_partial).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0 if all(r.get("ok", True) for r in rows) or rows else 1

    paths = _write_outputs(rows)
    _print_summary_table(rows)
    print("\nResults:")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
