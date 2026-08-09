"""
1) 为已有沪深300日K补公司名称（不重拉行情字段）
2) 拉取中证1000成分股一年日K，并附带公司名称
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
import jqdatasdk as jq
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
HS300_DIR = BASE / "hs300_stocks_daily_1y"
ZZ1000_DIR = BASE / "zz1000_stocks_daily_1y"

MIN_INTERVAL_SEC = 0.5
BATCH_SIZE = 50
ZZ1000_INDEX = "000852.XSHG"
FIELDS = ["open", "close", "high", "low", "volume", "money", "pre_close", "paused"]


def _resolve_range():
    end_env = os.getenv("JQDATA_PULL_END")
    start_env = os.getenv("JQDATA_PULL_START")
    if end_env and start_env:
        end = datetime.strptime(end_env, "%Y-%m-%d").date()
        start = datetime.strptime(start_env, "%Y-%m-%d").date()
    else:
        end = datetime(2026, 5, 1).date()
        start = end - timedelta(days=365)
    return start, end


def _throttle(last_call: float) -> float:
    wait = MIN_INTERVAL_SEC - (time.monotonic() - last_call)
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()


def _load_name_map(codes: List[str], last_call: float) -> tuple[Dict[str, str], float]:
    """一次 get_all_securities 取名称，避免按票重复请求行情字段。"""
    last_call = _throttle(last_call)
    all_sec = jq.get_all_securities(types=["stock"], date=None)
    last_call = time.monotonic()
    name_map: Dict[str, str] = {}
    if all_sec is not None and len(all_sec) > 0:
        for code, row in all_sec.iterrows():
            display = row.get("display_name") or row.get("name") or ""
            name_map[str(code)] = str(display).strip()

    missing = [c for c in codes if c not in name_map or not name_map[c]]
    for code in missing:
        last_call = _throttle(last_call)
        try:
            info = jq.get_security_info(code)
            last_call = time.monotonic()
            display = getattr(info, "display_name", None) or getattr(info, "name", None) or ""
            name_map[code] = str(display).strip()
        except Exception:
            last_call = time.monotonic()
            name_map.setdefault(code, "")
    return name_map, last_call


def _normalize_price_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index()
    if "index" in df.columns and "time" not in df.columns:
        df = df.rename(columns={"index": "time"})
    return df


def _ensure_name_column(df: pd.DataFrame, name_map: Dict[str, str]) -> pd.DataFrame:
    if "code" not in df.columns:
        return df
    names = df["code"].map(lambda c: name_map.get(str(c), ""))
    if "name" in df.columns:
        df = df.copy()
        df["name"] = names
        # 把 name 放到 code 后
        cols = list(df.columns)
        cols.remove("name")
        code_i = cols.index("code")
        cols.insert(code_i + 1, "name")
        return df[cols]
    df = df.copy()
    df.insert(df.columns.get_loc("code") + 1, "name", names)
    return df


def enrich_hs300(name_map: Dict[str, str]) -> None:
    if not HS300_DIR.exists():
        print("SKIP hs300 enrich: dir missing")
        return

    all_path = HS300_DIR / "all_hs300_daily_1y.csv"
    if all_path.exists():
        df = pd.read_csv(all_path)
        df = _ensure_name_column(df, name_map)
        df.to_csv(all_path, index=False, encoding="utf-8-sig")
        print(f"enriched {all_path} rows={len(df)}")

    n = 0
    for path in sorted(HS300_DIR.glob("*.csv")):
        if path.name in ("all_hs300_daily_1y.csv", "summary.csv"):
            continue
        df = pd.read_csv(path)
        if "code" not in df.columns:
            continue
        df = _ensure_name_column(df, name_map)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        n += 1
    print(f"enriched hs300 per-stock files={n}")

    # summary 增加 name
    summary_path = HS300_DIR / "summary.csv"
    if summary_path.exists():
        summary = pd.read_csv(summary_path)
        if "code" in summary.columns:
            summary = _ensure_name_column(summary, name_map)
            summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
            print(f"enriched {summary_path}")


def fetch_zz1000(name_map: Dict[str, str], start, end, last_call: float) -> float:
    ZZ1000_DIR.mkdir(parents=True, exist_ok=True)
    last_call = _throttle(last_call)
    stocks = jq.get_index_stocks(ZZ1000_INDEX, date=str(end))
    last_call = time.monotonic()
    if not stocks:
        raise RuntimeError("中证1000 成分股为空")

    stocks = sorted(set(stocks))
    (ZZ1000_DIR / "constituents.txt").write_text("\n".join(stocks) + "\n", encoding="utf-8")
    print(f"zz1000 constituents={len(stocks)}")

    # 补全名称图（成分里可能有 all_securities 未覆盖的）
    extra = [c for c in stocks if c not in name_map or not name_map[c]]
    if extra:
        more, last_call = _load_name_map(extra, last_call)
        name_map.update(more)

    frames = []
    failed: List[str] = []
    total_batches = (len(stocks) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(stocks), BATCH_SIZE):
        batch = stocks[i : i + BATCH_SIZE]
        batch_no = i // BATCH_SIZE + 1
        last_call = _throttle(last_call)
        t0 = time.monotonic()
        try:
            df = jq.get_price(
                batch,
                start_date=str(start),
                end_date=str(end),
                frequency="daily",
                fields=FIELDS,
                fq="pre",
                panel=False,
                fill_paused=True,
            )
        except Exception as exc:
            last_call = time.monotonic()
            print(f"BATCH FAIL {batch_no}/{total_batches}: {exc}")
            for code in batch:
                last_call = _throttle(last_call)
                try:
                    one = jq.get_price(
                        code,
                        start_date=str(start),
                        end_date=str(end),
                        frequency="daily",
                        fields=FIELDS,
                        fq="pre",
                        panel=False,
                        fill_paused=True,
                    )
                    last_call = time.monotonic()
                    if one is None or len(one) == 0:
                        failed.append(code)
                        continue
                    one = _normalize_price_df(one)
                    if "code" not in one.columns:
                        one.insert(0, "code", code)
                    one = _ensure_name_column(one, name_map)
                    frames.append(one)
                    one.to_csv(
                        ZZ1000_DIR / f"{code.replace('.', '_')}.csv",
                        index=False,
                        encoding="utf-8-sig",
                    )
                except Exception as one_exc:
                    last_call = time.monotonic()
                    failed.append(code)
                    print(f"  FAIL {code}: {one_exc}")
            continue

        last_call = time.monotonic()
        elapsed = last_call - t0
        if df is None or len(df) == 0:
            print(f"BATCH EMPTY {batch_no}/{total_batches}")
            failed.extend(batch)
            continue

        df = _normalize_price_df(df)
        if "code" not in df.columns:
            print(f"BATCH WARN {batch_no}: no code column")
            failed.extend(batch)
            continue

        df = _ensure_name_column(df, name_map)
        frames.append(df)
        for code, g in df.groupby("code"):
            g.to_csv(
                ZZ1000_DIR / f"{str(code).replace('.', '_')}.csv",
                index=False,
                encoding="utf-8-sig",
            )
        print(
            f"OK batch {batch_no}/{total_batches}: "
            f"stocks={df['code'].nunique()}/{len(batch)} rows={len(df)} secs={elapsed:.2f}"
        )

    if not frames:
        raise RuntimeError("中证1000 无行情数据")

    all_df = pd.concat(frames, ignore_index=True)
    all_path = ZZ1000_DIR / "all_zz1000_daily_1y.csv"
    all_df.to_csv(all_path, index=False, encoding="utf-8-sig")

    summary = (
        all_df.groupby(["code", "name"], dropna=False)
        .agg(rows=("time", "count"), start=("time", "min"), end=("time", "max"))
        .reset_index()
    )
    summary.to_csv(ZZ1000_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"zz1000 combined rows={len(all_df)} stocks={all_df['code'].nunique()} -> {all_path}")

    if failed:
        (ZZ1000_DIR / "failed.txt").write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"failed={len(failed)}")
    return last_call


def main() -> int:
    load_dotenv(ROOT / ".env")
    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        print("ERROR: missing JQDATA credentials", file=sys.stderr)
        return 1

    start, end = _resolve_range()
    print(f"auth user={user[:3]}***")
    jq.auth(user, pwd)
    print("auth ok")
    print(f"range={start} -> {end}")

    # 从已有 hs300 文件收集 code，名称一次拉全表
    codes: List[str] = []
    cons = HS300_DIR / "constituents.txt"
    if cons.exists():
        codes.extend([x.strip() for x in cons.read_text(encoding="utf-8").splitlines() if x.strip()])
    last_call = 0.0
    name_map, last_call = _load_name_map(codes, last_call)
    print(f"name_map size={len(name_map)}")

    print("--- enrich HS300 names (no price refetch) ---")
    enrich_hs300(name_map)

    print("--- fetch ZZ1000 constituents daily ---")
    fetch_zz1000(name_map, start, end, last_call)

    try:
        jq.logout()
    except Exception:
        pass
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
