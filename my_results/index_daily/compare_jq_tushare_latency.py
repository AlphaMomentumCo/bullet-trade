"""
对比 jqdata / tushare 在 mini_demo_index_universe 热路径上的接口延迟。

说明：
- Tushare 未实现 get_fundamentals，该项仅测 jq，并在报告中标注。
- 默认关闭磁盘缓存，测的是接口往返延迟（非 CacheManager 命中）。
- 调用间隔单独 sleep，不计入 latency，避免触发限流。

用法（quant）:
  python my_results/index_daily/compare_jq_tushare_latency.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

OUT_DIR = ROOT / "my_results" / "index_daily" / "latency_compare"
CONSTITUENTS = ROOT / "my_results/index_daily/hs300_stocks_daily_1y/constituents.txt"
# 与账号可用窗口对齐
END = os.getenv("BT_PROFILE_END", "2026-04-30")
START = os.getenv("BT_PROFILE_START", "2025-05-01")
SAMPLES = int(os.getenv("BT_LATENCY_SAMPLES", "15"))
# 调用间隔（不计入延迟）：tushare 积分接口常见约 200~500 次/分
GAP_SEC = float(os.getenv("BT_LATENCY_GAP", "0.35"))


def _stats(values: List[float]) -> Dict[str, Any]:
    if not values:
        return {"n": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    ordered = sorted(values)
    p95_i = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return {
        "n": len(values),
        "mean_ms": round(statistics.mean(values) * 1000, 2),
        "p50_ms": round(statistics.median(values) * 1000, 2),
        "p95_ms": round(ordered[p95_i] * 1000, 2),
        "min_ms": round(min(values) * 1000, 2),
        "max_ms": round(max(values) * 1000, 2),
    }


def _bench(name: str, fn: Callable[[], Any], n: int, gap: float) -> Dict[str, Any]:
    times: List[float] = []
    errors = 0
    last_err = None
    for i in range(n):
        if i > 0 and gap > 0:
            time.sleep(gap)
        t0 = time.perf_counter()
        try:
            fn()
            times.append(time.perf_counter() - t0)
        except Exception as exc:
            errors += 1
            last_err = f"{type(exc).__name__}: {exc}"
            times.append(time.perf_counter() - t0)  # 仍记录失败耗时，便于看超时
    result = {
        "api": name,
        "ok": n - errors,
        "errors": errors,
        "last_error": last_err,
        **_stats(times),
    }
    print(
        f"  {name:40s} mean={result['mean_ms']}ms p50={result['p50_ms']}ms "
        f"p95={result['p95_ms']}ms ok={result['ok']}/{n}"
        + (f" err={last_err}" if last_err else "")
    )
    return result


def _make_jq():
    from bullet_trade.data.providers.jqdata import JQDataProvider

    p = JQDataProvider(
        {
            "username": os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER"),
            "password": os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD"),
            "cache_dir": "",  # 关闭磁盘缓存
        }
    )
    p.auth()
    return p


def _make_tushare():
    from bullet_trade.data.providers.tushare import TushareProvider

    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未配置 TUSHARE_TOKEN（请写入 .env）")
    p = TushareProvider(
        {
            "token": token,
            "tushare_custom_url": os.getenv("TUSHARE_CUSTOM_URL") or None,
            "cache_dir": "",
        }
    )
    p.auth()
    return p


def _load_codes(n: int) -> List[str]:
    codes = [x.strip() for x in CONSTITUENTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    if not codes:
        raise RuntimeError(f"成分列表为空: {CONSTITUENTS}")
    # 均匀抽样，避免全是连续代码
    step = max(1, len(codes) // n)
    picked = codes[::step][:n]
    return picked


def run_provider(label: str, provider, codes: List[str]) -> Dict[str, Any]:
    print(f"\n=== {label} ===")
    end_dt = datetime.strptime(END, "%Y-%m-%d")
    start_dt = datetime.strptime(START, "%Y-%m-%d")
    rows: List[Dict[str, Any]] = []

    # 1) 交易日（引擎启动）
    rows.append(
        _bench(
            "get_trade_days",
            lambda: provider.get_trade_days(start_date=start_dt, end_date=end_dt),
            n=3,
            gap=GAP_SEC,
        )
    )

    # 2) 日K count=1 —— 对应 current_data / 日终估值点查
    idx = {"i": 0}

    def price_daily_count1():
        code = codes[idx["i"] % len(codes)]
        idx["i"] += 1
        return provider.get_price(
            code,
            end_date=end_dt,
            frequency="daily",
            fields=["open", "close", "high", "low", "volume", "money"],
            count=1,
            fq="pre",
            panel=True,
        )

    rows.append(_bench("get_price daily count=1", price_daily_count1, n=SAMPLES, gap=GAP_SEC))

    # 3) 日K open count=1 —— 对应 open 撮合 _resolve_base_exec_price
    idx2 = {"i": 0}

    def price_open_count1():
        code = codes[idx2["i"] % len(codes)]
        idx2["i"] += 1
        return provider.get_price(
            code,
            end_date=end_dt.replace(hour=9, minute=30),
            frequency="daily",
            fields=["open"],
            count=1,
            fq="none",
            panel=True,
        )

    rows.append(_bench("get_price open count=1", price_open_count1, n=SAMPLES, gap=GAP_SEC))

    # 4) 区间日K（一年）——对照块读 vs 点查
    code0 = codes[0]

    def price_range_1y():
        return provider.get_price(
            code0,
            start_date=start_dt,
            end_date=end_dt,
            frequency="daily",
            fields=["open", "close", "high", "low", "volume", "money"],
            fq="pre",
            panel=True,
        )

    rows.append(_bench("get_price daily 1y range", price_range_1y, n=3, gap=GAP_SEC))

    # 5) 分红
    idx3 = {"i": 0}

    def split_div():
        code = codes[idx3["i"] % len(codes)]
        idx3["i"] += 1
        return provider.get_split_dividend(code, start_date=start_dt, end_date=end_dt)

    rows.append(_bench("get_split_dividend", split_div, n=min(8, len(codes)), gap=GAP_SEC))

    # 6) 证券信息
    idx4 = {"i": 0}

    def sec_info():
        code = codes[idx4["i"] % len(codes)]
        idx4["i"] += 1
        return provider.get_security_info(code)

    rows.append(_bench("get_security_info", sec_info, n=min(10, len(codes)), gap=GAP_SEC))

    # 7) fundamentals —— 仅 jq 实现（策略选股路径）
    if label == "jqdata":
        from bullet_trade.data.providers import jqdata as jq_mod

        q = (
            jq_mod.query(jq_mod.jq.valuation.code, jq_mod.jq.valuation.market_cap)
            .filter(jq_mod.jq.valuation.market_cap.between(20, 30))
            .order_by(jq_mod.jq.valuation.market_cap.asc())
        )

        def fundamentals():
            return provider.get_fundamentals(q, date=end_dt.date())

        rows.append(_bench("get_fundamentals(market_cap)", fundamentals, n=3, gap=GAP_SEC))
    else:
        rows.append(
            {
                "api": "get_fundamentals(market_cap)",
                "ok": 0,
                "errors": 0,
                "last_error": "tushare 未实现 get_fundamentals（见 tushare.py NotImplementedError）",
                "n": 0,
                "mean_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "min_ms": None,
                "max_ms": None,
                "skipped": True,
            }
        )
        print("  get_fundamentals(market_cap)                 SKIPPED (not implemented)")

    return {"provider": label, "apis": rows}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    codes = _load_codes(SAMPLES)
    print(f"samples={SAMPLES} gap={GAP_SEC}s codes={len(codes)} range={START}->{END}")
    print(f"cache disabled; QPS spacing not included in latency")

    reports = []

    print("\ninit jqdata...")
    jq = _make_jq()
    reports.append(run_provider("jqdata", jq, codes))
    try:
        import jqdatasdk as jqsdk

        jqsdk.logout()
    except Exception:
        pass

    tushare_err = None
    try:
        print("\ninit tushare...")
        ts = _make_tushare()
        reports.append(run_provider("tushare", ts, codes))
    except Exception as exc:
        tushare_err = f"{type(exc).__name__}: {exc}"
        print(f"TUSHARE FAILED: {tushare_err}")
        reports.append({"provider": "tushare", "error": tushare_err, "apis": []})

    # 宽表对比表
    compare = []
    jq_map = {r["api"]: r for r in reports[0]["apis"]}
    ts_apis = reports[1].get("apis") or []
    ts_map = {r["api"]: r for r in ts_apis}
    for api in jq_map:
        j = jq_map[api]
        t = ts_map.get(api, {})
        compare.append(
            {
                "api": api,
                "jq_mean_ms": j.get("mean_ms"),
                "jq_p50_ms": j.get("p50_ms"),
                "jq_p95_ms": j.get("p95_ms"),
                "jq_ok": j.get("ok"),
                "ts_mean_ms": t.get("mean_ms"),
                "ts_p50_ms": t.get("p50_ms"),
                "ts_p95_ms": t.get("p95_ms"),
                "ts_ok": t.get("ok"),
                "ts_note": t.get("last_error") or ("skipped" if t.get("skipped") else None),
                "ratio_ts_over_jq": (
                    round(t["mean_ms"] / j["mean_ms"], 2)
                    if t.get("mean_ms") and j.get("mean_ms")
                    else None
                ),
            }
        )

    out = {
        "start": START,
        "end": END,
        "samples": SAMPLES,
        "gap_sec": GAP_SEC,
        "strategy": "mini_demo_index_universe hot-path APIs",
        "note": "Tushare 无 get_fundamentals；延迟为关闭磁盘缓存后的单次调用耗时",
        "reports": reports,
        "compare": compare,
    }
    out_path = OUT_DIR / "jq_vs_tushare_latency.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    import pandas as pd

    pd.DataFrame(compare).to_csv(OUT_DIR / "jq_vs_tushare_latency.csv", index=False, encoding="utf-8-sig")
    print(f"\nwrote {out_path}")
    print("\ncompare (mean_ms):")
    for row in compare:
        print(
            f"  {row['api']:36s} jq={row['jq_mean_ms']!s:>8}  "
            f"ts={row['ts_mean_ms']!s:>8}  ratio={row['ratio_ts_over_jq']}"
        )
    return 0 if not tushare_err else 2


if __name__ == "__main__":
    raise SystemExit(main())
