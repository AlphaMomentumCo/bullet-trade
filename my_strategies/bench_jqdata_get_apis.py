"""
测试 bullet_trade.data.providers.jqdata.JQDataProvider 中全部 get_* 接口耗时。

用法（项目根目录，quant 环境）:
  python my_strategies/bench_jqdata_get_apis.py

结果 CSV 默认写入:
  my_results/jqdata_api_latency/jqdata_get_apis_latency.csv
"""
from __future__ import annotations

import inspect
import json
import os
import statistics
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

OUT_DIR = ROOT / "my_results" / "jqdata_api_latency"
# 与当前账号常见可用窗口对齐，可用环境变量覆盖
END = os.getenv("BT_JQ_BENCH_END", "2026-04-30")
START = os.getenv("BT_JQ_BENCH_START", "2025-05-01")
SAMPLES = int(os.getenv("BT_JQ_BENCH_SAMPLES", "3"))
GAP_SEC = float(os.getenv("BT_JQ_BENCH_GAP", "0.35"))
STOCK = os.getenv("BT_JQ_BENCH_STOCK", "000001.XSHE")
INDEX = os.getenv("BT_JQ_BENCH_INDEX", "000300.XSHG")
FUND = os.getenv("BT_JQ_BENCH_FUND", "000001.OF")  # 场外基金代码
INDUSTRY = os.getenv("BT_JQ_BENCH_INDUSTRY", "HY001")
CONCEPT = os.getenv("BT_JQ_BENCH_CONCEPT", "")  # 空则运行时从 get_concepts 解析
FUTURE_UNDERLYING = os.getenv("BT_JQ_BENCH_FUTURE", "IF")

# 与 jqdata.JQDataProvider / tushare 基准 CSV 共用的固定输出顺序
API_ORDER = [
    "get_price",
    "get_security_info",
    "get_trade_days",
    "get_all_securities",
    "get_index_stocks",
    "get_bars",
    "get_ticks",
    "get_current_tick",
    "get_extras",
    "get_fundamentals",
    "get_fundamentals_continuously",
    "get_index_weights",
    "get_industry_stocks",
    "get_industry",
    "get_concept_stocks",
    "get_concept",
    "get_fund_info",
    "get_margincash_stocks",
    "get_marginsec_stocks",
    "get_dominant_future",
    "get_future_contracts",
    "get_billboard_list",
    "get_locked_shares",
    "get_trade_day",
    "get_live_current",
    "get_split_dividend",
]


def _stats(xs: List[float]) -> Dict[str, Any]:
    if not xs:
        return {
            "n": 0,
            "mean_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "min_ms": None,
            "max_ms": None,
        }
    xs_sorted = sorted(xs)
    p95_i = min(len(xs_sorted) - 1, max(0, int(round(0.95 * (len(xs_sorted) - 1)))))
    return {
        "n": len(xs),
        "mean_ms": round(statistics.mean(xs) * 1000, 3),
        "p50_ms": round(statistics.median(xs) * 1000, 3),
        "p95_ms": round(xs_sorted[p95_i] * 1000, 3),
        "min_ms": round(min(xs) * 1000, 3),
        "max_ms": round(max(xs) * 1000, 3),
    }


def _result_meta(value: Any) -> Tuple[str, Optional[int]]:
    if value is None:
        return "None", 0
    if isinstance(value, (list, tuple, set)):
        return type(value).__name__, len(value)
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            return "DataFrame", int(len(value))
        if isinstance(value, pd.Series):
            return "Series", int(len(value))
    except Exception:
        pass
    if isinstance(value, dict):
        return "dict", len(value)
    return type(value).__name__, None


_CONCEPT_CACHE: Dict[str, str] = {}


def _resolve_concept_code(provider, date) -> str:
    if CONCEPT:
        return CONCEPT
    key = str(date)
    if key in _CONCEPT_CACHE:
        return _CONCEPT_CACHE[key]
    # 从单票概念结果里取一个可用 code
    info = provider.get_concept(STOCK, date=date)
    code = CONCEPT or "GN001"
    try:
        if isinstance(info, dict):
            # 常见结构: {security: {'jq_concept': [{'concept_code': ..., ...}, ...]}}
            payload = info.get(STOCK) or next(iter(info.values()), {})
            concepts = payload.get("jq_concept") or payload.get("concept") or []
            if concepts:
                code = concepts[0].get("concept_code") or concepts[0].get("code") or code
    except Exception:
        pass
    _CONCEPT_CACHE[key] = str(code)
    return _CONCEPT_CACHE[key]


def _call_current_tick(provider, security: str):
    # 部分环境下长时间运行后 SDK 会话失效，失败时重认证一次
    try:
        return provider.get_current_tick(security)
    except Exception:
        provider.auth()
        return provider.get_current_tick(security)


def _make_cases(provider) -> List[Tuple[str, Callable[[], Any], str]]:
    """为每个 get_* 构造可调用用例 + 请求数据备注。"""
    from bullet_trade.data.providers import jqdata as jq_mod

    end_dt = datetime.strptime(END, "%Y-%m-%d")
    start_dt = datetime.strptime(START, "%Y-%m-%d")
    end_date = end_dt.date()
    date_range = f"{START}~{END}"

    q = (
        jq_mod.query(jq_mod.jq.valuation.code, jq_mod.jq.valuation.market_cap)
        .filter(jq_mod.jq.valuation.market_cap.between(20, 30))
        .order_by(jq_mod.jq.valuation.market_cap.asc())
    )
    fund_q_note = (
        f"valuation.code/market_cap, filter market_cap in [20,30]亿, "
        f"order by market_cap asc, date={end_date}"
    )

    cases: List[Tuple[str, Callable[[], Any], str]] = [
        (
            "get_price",
            lambda: provider.get_price(
                STOCK,
                start_date=start_dt,
                end_date=end_dt,
                frequency="daily",
                fields=["open", "close", "high", "low", "volume", "money"],
                fq="pre",
            ),
            f"日线OHLCV+成交额; security={STOCK}; {date_range}; fq=pre",
        ),
        (
            "get_security_info",
            lambda: provider.get_security_info(STOCK),
            f"证券基础信息; security={STOCK}",
        ),
        (
            "get_trade_days",
            lambda: provider.get_trade_days(start_date=start_dt, end_date=end_dt),
            f"交易日列表; {date_range}",
        ),
        (
            "get_all_securities",
            lambda: provider.get_all_securities(types="stock", date=end_date),
            f"全部A股列表; types=stock; date={end_date}",
        ),
        (
            "get_index_stocks",
            lambda: provider.get_index_stocks(INDEX, date=end_date),
            f"指数成分股; index={INDEX}; date={end_date}",
        ),
        (
            "get_bars",
            lambda: provider.get_bars(
                STOCK,
                count=10,
                unit="1d",
                fields=["open", "close", "high", "low", "volume"],
                end_dt=end_dt,
                fq_ref_date=end_date,
                df=True,
            ),
            f"最近10根日K OHLCV; security={STOCK}; end_dt={end_date}; unit=1d; fq_ref_date={end_date}",
        ),
        (
            "get_ticks",
            lambda: provider.get_ticks(
                STOCK,
                end_dt=end_dt.replace(hour=15, minute=0),
                count=20,
                df=True,
            ),
            f"历史tick最近20条; security={STOCK}; end_dt={end_date} 15:00",
        ),
        (
            "get_current_tick",
            lambda: _call_current_tick(provider, STOCK),
            f"实时tick快照; security={STOCK}",
        ),
        (
            "get_extras",
            lambda: provider.get_extras(
                "is_st",
                [STOCK],
                start_date=start_dt,
                end_date=end_dt,
                df=True,
            ),
            f"附加字段 is_st; security={STOCK}; {date_range}",
        ),
        (
            "get_fundamentals",
            lambda: provider.get_fundamentals(q, date=end_date),
            f"单日基本面查询; {fund_q_note}",
        ),
        (
            "get_fundamentals_continuously",
            lambda: provider.get_fundamentals_continuously(q, end_date=end_date, count=5, panel=False),
            f"连续5日基本面; {fund_q_note}; count=5; panel=False",
        ),
        (
            "get_index_weights",
            lambda: provider.get_index_weights(INDEX, date=end_date),
            f"指数权重; index={INDEX}; date={end_date}",
        ),
        (
            "get_industry_stocks",
            lambda: provider.get_industry_stocks(INDUSTRY, date=end_date),
            f"行业成分股; industry={INDUSTRY}; date={end_date}",
        ),
        (
            "get_industry",
            lambda: provider.get_industry(STOCK, date=end_date),
            f"证券所属行业; security={STOCK}; date={end_date}",
        ),
        (
            "get_concept_stocks",
            lambda: provider.get_concept_stocks(_resolve_concept_code(provider, end_date), date=end_date),
            f"概念成分股; concept=运行时从 get_concept({STOCK}) 解析或 BT_JQ_BENCH_CONCEPT; date={end_date}",
        ),
        (
            "get_concept",
            lambda: provider.get_concept(STOCK, date=end_date),
            f"证券所属概念; security={STOCK}; date={end_date}",
        ),
        (
            "get_fund_info",
            lambda: provider.get_fund_info(FUND, date=end_date),
            f"基金信息; fund={FUND}; date={end_date}",
        ),
        (
            "get_margincash_stocks",
            lambda: provider.get_margincash_stocks(date=end_date),
            f"融资标的列表; date={end_date}",
        ),
        (
            "get_marginsec_stocks",
            lambda: provider.get_marginsec_stocks(date=end_date),
            f"融券标的列表; date={end_date}",
        ),
        (
            "get_dominant_future",
            lambda: provider.get_dominant_future(FUTURE_UNDERLYING, date=end_date),
            f"主力合约; underlying={FUTURE_UNDERLYING}; date={end_date}",
        ),
        (
            "get_future_contracts",
            lambda: provider.get_future_contracts(FUTURE_UNDERLYING, date=end_date),
            f"期货合约列表; underlying={FUTURE_UNDERLYING}; date={end_date}",
        ),
        (
            "get_billboard_list",
            lambda: provider.get_billboard_list(
                stock_list=[STOCK],
                start_date=start_dt,
                end_date=end_dt,
            ),
            f"龙虎榜; stock_list=[{STOCK}]; {date_range}",
        ),
        (
            "get_locked_shares",
            lambda: provider.get_locked_shares(
                stock_list=[STOCK],
                start_date=start_dt,
                end_date=end_dt,
            ),
            f"限售解禁; stock_list=[{STOCK}]; {date_range}",
        ),
        (
            "get_trade_day",
            lambda: provider.get_trade_day(STOCK, end_dt),
            f"查询日对应交易日; security={STOCK}; query_dt={end_date}",
        ),
        (
            "get_live_current",
            lambda: provider.get_live_current(STOCK),
            f"实盘当前快照(last_price/涨跌停等); security={STOCK}",
        ),
        (
            "get_split_dividend",
            lambda: provider.get_split_dividend(STOCK, start_date=start_dt, end_date=end_dt),
            f"拆分送转与分红; security={STOCK}; {date_range}",
        ),
    ]
    return cases


def _discover_get_methods(provider) -> List[str]:
    names = []
    for name, _member in inspect.getmembers(provider, predicate=inspect.ismethod):
        if name.startswith("get_") and not name.startswith("get__"):
            names.append(name)
    # 也包含未绑定到实例但定义在类上的
    for name, _member in inspect.getmembers(type(provider), predicate=inspect.isfunction):
        if name.startswith("get_") and name not in names:
            names.append(name)
    return sorted(set(names))


def bench_one(
    name: str,
    fn: Callable[[], Any],
    samples: int,
    gap: float,
    request_note: str = "",
) -> Dict[str, Any]:
    times: List[float] = []
    ok = 0
    errors = 0
    last_error = None
    result_type = None
    result_size = None

    for i in range(samples):
        if i > 0 and gap > 0:
            time.sleep(gap)
        t0 = time.perf_counter()
        try:
            value = fn()
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            ok += 1
            result_type, result_size = _result_meta(value)
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"

    st = _stats(times)
    row = {
        "api": name,
        "request_note": request_note,
        "samples": samples,
        "ok": ok,
        "errors": errors,
        "last_error": last_error,
        "result_type": result_type,
        "result_size": result_size,
        **st,
    }
    status = "OK" if errors == 0 else ("PARTIAL" if ok else "FAIL")
    print(
        f"[{status:7s}] {name:32s} mean={row['mean_ms']}ms "
        f"p50={row['p50_ms']}ms ok={ok}/{samples}"
        + (f" | {last_error}" if last_error else "")
    )
    return row


def main() -> int:
    from bullet_trade.data.providers.jqdata import JQDataProvider

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    provider = JQDataProvider(
        {
            "username": os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER"),
            "password": os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD"),
            "server": os.getenv("JQDATA_SERVER") or None,
            "port": int(os.getenv("JQDATA_PORT") or 0) or None,
            "cache_dir": "",  # 关闭磁盘缓存，测接口本身
        }
    )
    print("auth...")
    provider.auth()
    print("auth ok")
    print(f"range={START}->{END} samples={SAMPLES} gap={GAP_SEC}s stock={STOCK}")
    print("cache_dir disabled")

    declared = _discover_get_methods(provider)
    cases = _make_cases(provider)
    case_map = {name: (fn, note) for name, fn, note in cases}

    # 概念成分股备注补上实际 concept code
    end_date = datetime.strptime(END, "%Y-%m-%d").date()
    try:
        concept_code = _resolve_concept_code(provider, end_date)
        fn, note = case_map["get_concept_stocks"]
        case_map["get_concept_stocks"] = (
            fn,
            f"概念成分股; concept={concept_code}; date={end_date}",
        )
    except Exception:
        pass

    missing = [n for n in declared if n not in case_map]
    extra = [n for n in case_map if n not in declared]
    if missing:
        print(f"WARNING: discovered but no case: {missing}")
    if extra:
        print(f"WARNING: case not on provider: {extra}")

    rows: List[Dict[str, Any]] = []
    # 固定 API 顺序（与 tushare 基准一致），再补遗漏
    ordered = [n for n in API_ORDER if n in case_map]
    ordered += [n for n in declared if n in case_map and n not in ordered]
    ordered += [n for n in case_map if n not in ordered]
    for name in ordered:
        fn, note = case_map[name]
        rows.append(bench_one(name, fn, SAMPLES, GAP_SEC, request_note=note))

    # 未覆盖的 get_* 也写入一行，便于完整清单
    for name in missing:
        rows.append(
            {
                "api": name,
                "request_note": "",
                "samples": 0,
                "ok": 0,
                "errors": 0,
                "last_error": "no benchmark case defined",
                "result_type": None,
                "result_size": None,
                "n": 0,
                "mean_ms": None,
                "p50_ms": None,
                "p95_ms": None,
                "min_ms": None,
                "max_ms": None,
            }
        )

    import pandas as pd

    df = pd.DataFrame(rows)
    # 输出顺序与 API_ORDER 对齐（便于 jq / tushare 横向对比）
    rank = {name: i for i, name in enumerate(API_ORDER)}
    df["_rank"] = df["api"].map(lambda a: rank.get(a, 10_000))
    df = df.sort_values(["_rank", "api"]).drop(columns=["_rank"]).reset_index(drop=True)
    cols = [
        "api",
        "request_note",
        "samples",
        "ok",
        "errors",
        "last_error",
        "result_type",
        "result_size",
        "n",
        "mean_ms",
        "p50_ms",
        "p95_ms",
        "min_ms",
        "max_ms",
    ]
    df = df[[c for c in cols if c in df.columns]]
    csv_path = OUT_DIR / "jqdata_get_apis_latency.csv"
    json_path = OUT_DIR / "jqdata_get_apis_latency.json"
    meta = {
        "start": START,
        "end": END,
        "samples": SAMPLES,
        "gap_sec": GAP_SEC,
        "stock": STOCK,
        "index": INDEX,
        "cache_disabled": True,
        "discovered_get_apis": declared,
        "rows": rows,
    }

    def _write_outputs(csv_p: Path, json_p: Path) -> None:
        df.to_csv(csv_p, index=False, encoding="utf-8-sig")
        json_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        _write_outputs(csv_path, json_path)
    except PermissionError:
        # 常见原因：CSV 正被 Excel 打开
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = OUT_DIR / f"jqdata_get_apis_latency_{stamp}.csv"
        json_path = OUT_DIR / f"jqdata_get_apis_latency_{stamp}.json"
        _write_outputs(csv_path, json_path)
        print(f"WARNING: 原 CSV 被占用，已改写到带时间戳文件")

    try:
        import jqdatasdk as jq

        jq.logout()
    except Exception:
        pass

    print(f"\nCSV  -> {csv_path}")
    print(f"JSON -> {json_path}")
    print(f"APIs tested: {len(rows)} / discovered: {len(declared)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
