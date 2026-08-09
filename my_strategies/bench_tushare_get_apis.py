"""
测试 bullet_trade.data.providers.tushare.TushareProvider 中全部 get_* 接口耗时。

用法（项目根目录，quant 环境）:
  python my_strategies/bench_tushare_get_apis.py

结果 CSV 默认写入:
  my_results/tushare_api_latency/tushare_get_apis_latency.csv
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

OUT_DIR = ROOT / "my_results" / "tushare_api_latency"
RESULT_DIR = OUT_DIR / "api_results"
# 默认使用本地磁盘缓存目录（可用 BT_TS_BENCH_CACHE_DIR / DATA_CACHE_DIR 覆盖）
_raw_cache = os.getenv("BT_TS_BENCH_CACHE_DIR") or os.getenv("DATA_CACHE_DIR") or str(OUT_DIR / "cache")
CACHE_DIR = os.path.expanduser(_raw_cache)
MEM_CACHE = os.getenv("BT_TS_BENCH_MEM_CACHE", "1") not in ("0", "false", "False")
END = os.getenv("BT_TS_BENCH_END", "2026-04-30")
START = os.getenv("BT_TS_BENCH_START", "2025-05-01")
SAMPLES = int(os.getenv("BT_TS_BENCH_SAMPLES", "3"))
GAP_SEC = float(os.getenv("BT_TS_BENCH_GAP", "0.35"))
STOCK = os.getenv("BT_TS_BENCH_STOCK", "000001.XSHE")
INDEX = os.getenv("BT_TS_BENCH_INDEX", "000300.XSHG")
FUND = os.getenv("BT_TS_BENCH_FUND", "000001.OF")
INDUSTRY = os.getenv("BT_TS_BENCH_INDUSTRY", "")  # 空则运行时取申万一级
CONCEPT = os.getenv("BT_TS_BENCH_CONCEPT", "")
FUTURE_UNDERLYING = os.getenv("BT_TS_BENCH_FUTURE", "IF")
RESULT_PREVIEW_CHARS = int(os.getenv("BT_TS_BENCH_PREVIEW_CHARS", "800"))
RESULT_MAX_ROWS = int(os.getenv("BT_TS_BENCH_RESULT_ROWS", "50"))

# 与 jqdata 基准 CSV 共用的固定输出顺序
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
_INDUSTRY_CACHE: Dict[str, str] = {}


def _resolve_concept_code(provider, date) -> str:
    if CONCEPT:
        return CONCEPT
    key = str(date)
    if key in _CONCEPT_CACHE:
        return _CONCEPT_CACHE[key]
    code = ""
    # 优先用概念目录，避免为解析 code 提前打 get_concept（污染耗时）
    try:
        if hasattr(provider, "_normalize_concept_code"):
            code = provider._normalize_concept_code("")
        else:
            cat = provider._ensure_client().concept(src="ts")
            code = str(cat.iloc[0].get("code") or cat.iloc[0].get("id") or "")
    except Exception:
        code = ""
    _CONCEPT_CACHE[key] = code or ""
    return _CONCEPT_CACHE[key]


def _preview_value(value: Any, max_chars: int = RESULT_PREVIEW_CHARS) -> str:
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            head = value.head(min(5, len(value)))
            text = head.to_csv(index=True)
            return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
        if isinstance(value, pd.Series):
            text = value.head(20).to_csv(index=True)
            return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
    except Exception:
        pass
    if isinstance(value, (list, tuple)):
        sample = list(value)[:20]
        text = json.dumps(sample, ensure_ascii=False, default=str)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."
    text = str(value)
    return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def _save_api_result(api_name: str, value: Any) -> str:
    """将接口结果落到 api_results/，返回相对路径备注。"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    safe = api_name.replace("/", "_")
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            path = RESULT_DIR / f"{safe}.csv"
            value.head(RESULT_MAX_ROWS).to_csv(path, index=True, encoding="utf-8-sig")
            return str(path.relative_to(OUT_DIR))
        if isinstance(value, pd.Series):
            path = RESULT_DIR / f"{safe}.csv"
            value.head(RESULT_MAX_ROWS).to_frame("value").to_csv(path, encoding="utf-8-sig")
            return str(path.relative_to(OUT_DIR))
    except Exception:
        pass

    path = RESULT_DIR / f"{safe}.json"
    payload: Any
    if isinstance(value, (list, tuple, set)):
        payload = {
            "type": type(value).__name__,
            "size": len(value),
            "sample": list(value)[:RESULT_MAX_ROWS],
        }
    elif isinstance(value, dict):
        # 大 dict 截断
        items = list(value.items())
        payload = {
            "type": "dict",
            "size": len(value),
            "sample": {k: v for k, v in items[:RESULT_MAX_ROWS]},
        }
    else:
        payload = {"type": type(value).__name__, "value": value}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return str(path.relative_to(OUT_DIR))


def _resolve_industry_code(provider) -> str:
    if INDUSTRY:
        return INDUSTRY
    if "default" in _INDUSTRY_CACHE:
        return _INDUSTRY_CACHE["default"]
    code = "801010.SI"
    try:
        pro = provider._ensure_client()
        classify = pro.index_classify(level="L1", src="SW2021")
        if classify is not None and not classify.empty:
            code = str(classify.iloc[0].get("index_code") or classify.iloc[0].get("code") or code)
    except Exception:
        pass
    _INDUSTRY_CACHE["default"] = code
    return code


def _make_cases(provider) -> List[Tuple[str, Callable[[], Any], str]]:
    end_dt = datetime.strptime(END, "%Y-%m-%d")
    start_dt = datetime.strptime(START, "%Y-%m-%d")
    end_date = end_dt.date()
    date_range = f"{START}~{END}"
    industry_code = _resolve_industry_code(provider)

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
            f"最近10根日K OHLCV; security={STOCK}; end_dt={end_date}; unit=1d",
        ),
        (
            "get_ticks",
            lambda: provider.get_ticks(
                STOCK,
                end_dt=end_dt.replace(hour=15, minute=0),
                count=20,
                df=True,
            ),
            f"历史tick: get_tick_data({STOCK.split('.')[0]}, date={end_date}, src=tt) "
            f"time<=15:00:00 then tail(20)",
        ),
        (
            "get_current_tick",
            lambda: provider.get_current_tick(STOCK),
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
            lambda: provider.get_fundamentals(None, date=end_date),
            f"单日估值(daily_basic->market_cap亿元); date={end_date}",
        ),
        (
            "get_fundamentals_continuously",
            lambda: provider.get_fundamentals_continuously(None, end_date=end_date, count=5, panel=False),
            f"连续5日估值(daily_basic); end_date={end_date}; count=5",
        ),
        (
            "get_index_weights",
            lambda: provider.get_index_weights(INDEX, date=end_date),
            f"指数权重; index={INDEX}; date={end_date}",
        ),
        (
            "get_industry_stocks",
            lambda: provider.get_industry_stocks(industry_code, date=end_date),
            f"行业成分股; industry={industry_code}; date={end_date}",
        ),
        (
            "get_industry",
            lambda: provider.get_industry(STOCK, date=end_date),
            f"证券所属行业; security={STOCK}; date={end_date}",
        ),
        (
            "get_concept_stocks",
            lambda: provider.get_concept_stocks(_resolve_concept_code(provider, end_date), date=end_date),
            f"概念成分股; concept=运行时解析; date={end_date}",
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
    result_preview = ""
    result_file = ""
    last_value: Any = None

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
            last_value = value
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"

    if last_value is not None:
        try:
            result_preview = _preview_value(last_value)
            result_file = _save_api_result(name, last_value)
        except Exception as exc:
            result_preview = f"<save_failed: {exc}>"

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
        "result_file": result_file,
        "result_preview": result_preview,
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
    from bullet_trade.data.providers.tushare import TushareProvider

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        raise RuntimeError("未配置 TUSHARE_TOKEN")

    custom_url = os.getenv("TUSHARE_CUSTOM_URL") or None
    if custom_url:
        custom_url = custom_url.strip().strip('"').strip("'")

    provider = TushareProvider(
        {
            "token": token,
            "tushare_custom_url": custom_url,
            "cache_dir": CACHE_DIR,
            "mem_cache": MEM_CACHE,
            "tick_src": os.getenv("TUSHARE_TICK_SRC", "tt"),
        }
    )
    print("auth...")
    provider.auth()
    print("auth ok")
    print(f"range={START}->{END} samples={SAMPLES} gap={GAP_SEC}s stock={STOCK}")
    print(f"custom_url={custom_url or '(default)'}")
    print(f"disk_cache={CACHE_DIR}")
    print(f"mem_cache={MEM_CACHE}")

    # 预热 stock_basic 全表（mem），让 get_security_info/get_industry 测的是热路径
    if MEM_CACHE:
        try:
            t0 = time.perf_counter()
            _ = provider._stock_basic_row(provider._to_ts_code(STOCK))
            print(f"warmup stock_basic_map: {round((time.perf_counter()-t0)*1000,1)}ms")
        except Exception as exc:
            print(f"warmup stock_basic_map failed: {exc}")

    declared = _discover_get_methods(provider)
    cases = _make_cases(provider)
    case_map = {name: (fn, note) for name, fn, note in cases}

    end_date = datetime.strptime(END, "%Y-%m-%d").date()
    try:
        concept_code = _resolve_concept_code(provider, end_date)
        fn, _note = case_map["get_concept_stocks"]
        case_map["get_concept_stocks"] = (
            lambda code=concept_code: provider.get_concept_stocks(code, date=end_date),
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
    ordered = [n for n in API_ORDER if n in case_map]
    ordered += [n for n in declared if n in case_map and n not in ordered]
    ordered += [n for n in case_map if n not in ordered]
    for name in ordered:
        fn, note = case_map[name]
        rows.append(bench_one(name, fn, SAMPLES, GAP_SEC, request_note=note))

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
                "result_file": "",
                "result_preview": "",
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
        "result_file",
        "result_preview",
        "n",
        "mean_ms",
        "p50_ms",
        "p95_ms",
        "min_ms",
        "max_ms",
    ]
    df = df[[c for c in cols if c in df.columns]]
    csv_path = OUT_DIR / "tushare_get_apis_latency.csv"
    json_path = OUT_DIR / "tushare_get_apis_latency.json"
    meta = {
        "provider": "tushare",
        "start": START,
        "end": END,
        "samples": SAMPLES,
        "gap_sec": GAP_SEC,
        "stock": STOCK,
        "index": INDEX,
        "custom_url": custom_url,
        "cache_dir": CACHE_DIR,
        "mem_cache": MEM_CACHE,
        "result_dir": str(RESULT_DIR),
        "discovered_get_apis": declared,
        "rows": rows,
    }

    def _write_outputs(csv_p: Path, json_p: Path) -> None:
        df.to_csv(csv_p, index=False, encoding="utf-8-sig")
        json_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        _write_outputs(csv_path, json_path)
    except PermissionError:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = OUT_DIR / f"tushare_get_apis_latency_{stamp}.csv"
        json_path = OUT_DIR / f"tushare_get_apis_latency_{stamp}.json"
        _write_outputs(csv_path, json_path)
        print("WARNING: 原 CSV 被占用，已改写到带时间戳文件")

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
