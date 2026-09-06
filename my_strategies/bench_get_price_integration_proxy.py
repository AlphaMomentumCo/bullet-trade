"""经 tushare-integration proxy 测 get_price 全量宇宙耗时。

宇宙: hs300 / zz1000 / all（A 股上市）
窗口: 1y / 5y（可用环境变量覆盖）

用法（项目根，quant）:
  # 默认: 三宇宙 × 1y+5y 全量成分
  python my_strategies/bench_get_price_integration_proxy.py

  BT_IU_UNIVERSES=hs300,zz1000,all
  BT_IU_WINDOWS=1y,5y
  BT_IU_SAMPLES=2
  BT_IU_PANEL=0          # 大宇宙建议 0，只测拉取+复权，不拼 MultiIndex panel
  BT_INTEGRATION_PROXY_URL=http://127.0.0.1:8000
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env", override=True)

PROXY_URL = (os.getenv("BT_INTEGRATION_PROXY_URL") or "http://127.0.0.1:8000").strip().strip('"').strip("'")
os.environ["TUSHARE_CUSTOM_URL"] = PROXY_URL
os.environ["TUSHARE_CLICKHOUSE"] = "false"

OUT = ROOT / "my_results" / "index_universe_link_latency"
OUT.mkdir(parents=True, exist_ok=True)

END = os.getenv("BT_IU_END", "2026-04-30")
SAMPLES = int(os.getenv("BT_IU_SAMPLES", "2"))
PANEL = os.getenv("BT_IU_PANEL", "0").strip().lower() not in {"0", "false", "no", ""}

UNIVERSES = [
    x.strip()
    for x in (os.getenv("BT_IU_UNIVERSES") or "hs300,zz1000,all").split(",")
    if x.strip()
]

# 窗口名 → (start, end)
_WINDOW_PRESETS = {
    "1y": (os.getenv("BT_IU_START_1Y", "2025-05-22"), END),
    "5y": (os.getenv("BT_IU_START_5Y", "2021-04-30"), END),
}
_win_raw = (os.getenv("BT_IU_WINDOWS") or "1y,5y").split(",")
WINDOWS: List[Tuple[str, str, str]] = []
for w in _win_raw:
    key = w.strip().lower()
    if not key:
        continue
    if key in _WINDOW_PRESETS:
        s, e = _WINDOW_PRESETS[key]
        WINDOWS.append((key, s, e))
    else:
        raise SystemExit(f"unknown window {key}, use 1y/5y")


def _load_univ_local(key: str) -> List[str]:
    name = {"hs300": "hs300_stocks_daily_1y", "zz1000": "zz1000_stocks_daily_1y"}[key]
    d = ROOT / "my_results" / "index_daily" / name
    out: List[str] = []
    for p in d.glob("*.csv"):
        stem = p.stem
        if "_" in stem and "." not in stem:
            code, suf = stem.rsplit("_", 1)
            stem = f"{code}.{suf}"
        if "." in stem:
            out.append(stem)
    return sorted(set(out))


def _load_all_stocks(provider) -> List[str]:
    """全市场：优先库内有日线的标的；其次 stock_basic；再退回 hs300∪zz1000。"""
    # 1) 有行情的代码（避免对空票空转）
    try:
        import clickhouse_connect

        pwd = os.getenv("TUSHARE_CLICKHOUSE_PASSWORD") or os.getenv("BT_CH_PASSWORD") or ""
        if not pwd:
            cfg = ROOT / "tushare-integration" / "config.yaml"
            if cfg.is_file():
                import yaml

                raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                pwd = str((raw.get("database") or {}).get("password") or "")
        client = clickhouse_connect.get_client(
            host=os.getenv("TUSHARE_CLICKHOUSE_HOST", "127.0.0.1"),
            port=int(os.getenv("TUSHARE_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("TUSHARE_CLICKHOUSE_USER", "default"),
            password=pwd,
            database=os.getenv("TUSHARE_CLICKHOUSE_DB", "default"),
        )
        for table in ("bt_daily_adj_fast", "bt_daily_fast", "daily"):
            try:
                r = client.query(f"SELECT DISTINCT ts_code FROM {table}")
                codes = []
                for (c,) in r.result_rows:
                    c = str(c)
                    if c.endswith(".SH"):
                        codes.append(c.replace(".SH", ".XSHG"))
                    elif c.endswith(".SZ"):
                        codes.append(c.replace(".SZ", ".XSHE"))
                    else:
                        codes.append(c)
                if codes:
                    print(f"  all from {table} distinct n={len(set(codes))}")
                    return sorted(set(codes))
            except Exception:
                continue
    except Exception as exc:
        print(f"  CH distinct ts_code failed: {exc}")

    try:
        pro = provider._ensure_client()
        df = pro.stock_basic(list_status="L", fields="ts_code,list_status")
        if df is not None and not df.empty and "ts_code" in df.columns:
            codes = []
            for c in df["ts_code"].astype(str):
                if c.endswith(".SH"):
                    codes.append(c.replace(".SH", ".XSHG"))
                elif c.endswith(".SZ"):
                    codes.append(c.replace(".SZ", ".XSHE"))
                elif c.endswith(".BJ"):
                    codes.append(c.replace(".BJ", ".XBEI"))
                else:
                    codes.append(c)
            if os.getenv("TUSHARE_INCLUDE_BSE", "0") not in ("1", "true", "True"):
                codes = [c for c in codes if not c.endswith(".XBEI")]
            print(f"  all from stock_basic n={len(set(codes))}")
            return sorted(set(codes))
    except Exception as exc:
        print(f"  stock_basic via proxy failed: {exc}")
    merged = _load_univ_local("hs300") + _load_univ_local("zz1000")
    print(f"  all fallback: hs300∪zz1000 n={len(set(merged))}")
    return sorted(set(merged))


def _run_one(provider, label: str, stocks: List[str], start: str, end: str) -> Dict:
    print(f"\n=== {label} n={len(stocks)} {start}->{end} panel={PANEL} ===", flush=True)
    if not stocks:
        return {
            "label": label,
            "n_sample": 0,
            "start": start,
            "end": end,
            "skipped": True,
        }

    kw = dict(
        start_date=start,
        end_date=end,
        frequency="daily",
        fq="pre",
        panel=PANEL,
    )

    t0 = time.perf_counter()
    df = provider.get_price(stocks, **kw)
    cold_ms = (time.perf_counter() - t0) * 1000
    shape = None if df is None else list(getattr(df, "shape", ()))
    nrows = 0 if df is None or getattr(df, "empty", True) else len(df)
    print(f"  cold_ms={cold_ms:.1f} shape={shape} nrows={nrows}", flush=True)

    hots: List[float] = []
    for i in range(SAMPLES):
        t0 = time.perf_counter()
        provider.get_price(stocks, **kw)
        hots.append((time.perf_counter() - t0) * 1000)
        print(f"  hot[{i}]={hots[-1]:.1f}ms", flush=True)
    p50 = statistics.median(hots)
    print(f"  hot_p50={p50:.1f}", flush=True)
    return {
        "label": label,
        "universe": label.split("@")[0],
        "window": label.split("@")[1] if "@" in label else "",
        "n_sample": len(stocks),
        "start": start,
        "end": end,
        "panel": PANEL,
        "cold_ms": round(cold_ms, 1),
        "hot_p50_ms": round(p50, 1),
        "hot_ms": [round(x, 1) for x in hots],
        "shape": shape,
        "nrows": nrows,
        "mode": "integration_proxy",
        "skipped": False,
    }


def main() -> None:
    from bullet_trade.data.providers.tushare import TushareProvider

    p = TushareProvider(
        {
            "token": os.getenv("TUSHARE_TOKEN") or "local-token",
            "tushare_custom_url": PROXY_URL,
            "cache_dir": "",
            "mem_cache": False,
            "clickhouse": {"enabled": False},
        }
    )
    print(
        f"mode=integration_proxy url={PROXY_URL} ch={p._ch_available()} "
        f"universes={UNIVERSES} windows={[w[0] for w in WINDOWS]} samples={SAMPLES} panel={PANEL}"
    )

    cache: Dict[str, List[str]] = {}
    rows = []
    for uni in UNIVERSES:
        if uni in ("hs300", "zz1000"):
            cache[uni] = _load_univ_local(uni)
        elif uni == "all":
            cache[uni] = _load_all_stocks(p)
        else:
            print(f"skip unknown universe {uni}")
            continue
        print(f"universe {uni}: n={len(cache[uni])}")

    for uni in UNIVERSES:
        stocks = cache.get(uni) or []
        for wname, start, end in WINDOWS:
            rows.append(_run_one(p, f"{uni}@{wname}", stocks, start, end))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "integration_proxy",
        "proxy_url": PROXY_URL,
        "samples": SAMPLES,
        "panel": PANEL,
        "universes": {k: len(v) for k, v in cache.items()},
        "rows": rows,
    }
    latest = OUT / "get_price_proxy_full_universe_latest.json"
    stamped = OUT / f"get_price_proxy_full_universe_{stamp}.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    latest.write_text(text, encoding="utf-8")
    stamped.write_text(text, encoding="utf-8")

    csv_path = OUT / "get_price_proxy_full_universe_latest.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        fields = [
            "universe",
            "window",
            "n_sample",
            "start",
            "end",
            "panel",
            "cold_ms",
            "hot_p50_ms",
            "shape",
            "mode",
        ]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if r.get("skipped"):
                continue
            w.writerow(
                {
                    **r,
                    "shape": str(r.get("shape")),
                }
            )
    print("wrote", latest)
    print("wrote", csv_path)


if __name__ == "__main__":
    main()
