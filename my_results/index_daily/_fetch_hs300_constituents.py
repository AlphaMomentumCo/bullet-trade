"""拉取沪深300全部成分股近一年日K（按 jqdatasdk QPS 分批）。"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
import jqdatasdk as jq
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent / "hs300_stocks_daily_1y"

# 官方约 30 次/秒；保守 2 QPS
MIN_INTERVAL_SEC = 0.5
# 单次请求约 50*242 ≈ 1.2 万行，远低于建议 10 万条上限
BATCH_SIZE = 50
INDEX_CODE = "000300.XSHG"
FIELDS = ["open", "close", "high", "low", "volume", "money", "pre_close", "paused"]


def _resolve_range() -> tuple:
    end_env = os.getenv("JQDATA_PULL_END")
    start_env = os.getenv("JQDATA_PULL_START")
    if end_env and start_env:
        end = datetime.strptime(end_env, "%Y-%m-%d").date()
        start = datetime.strptime(start_env, "%Y-%m-%d").date()
    else:
        # 与当前账号可用窗口对齐
        end = datetime(2026, 5, 1).date()
        start = end - timedelta(days=365)
    return start, end


def _throttle(last_call: float) -> float:
    wait = MIN_INTERVAL_SEC - (time.monotonic() - last_call)
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()


def main() -> int:
    load_dotenv(ROOT / ".env")
    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        print("ERROR: .env 缺少 JQDATA 账号", file=sys.stderr)
        return 1

    start, end = _resolve_range()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"auth user={user[:3]}***")
    jq.auth(user, pwd)
    print("auth ok")
    print(f"index={INDEX_CODE} range={start} -> {end}")
    print(f"qps guard={MIN_INTERVAL_SEC}s batch_size={BATCH_SIZE}")

    last_call = 0.0
    last_call = _throttle(last_call)
    # 成分以区间末日为准（权限末日附近）
    stocks = jq.get_index_stocks(INDEX_CODE, date=str(end))
    last_call = time.monotonic()
    if not stocks:
        print("ERROR: 成分股列表为空", file=sys.stderr)
        return 2

    stocks = sorted(set(stocks))
    print(f"constituents={len(stocks)}")
    (OUT_DIR / "constituents.txt").write_text("\n".join(stocks) + "\n", encoding="utf-8")

    frames = []
    failed = []
    for i in range(0, len(stocks), BATCH_SIZE):
        batch = stocks[i : i + BATCH_SIZE]
        batch_no = i // BATCH_SIZE + 1
        total_batches = (len(stocks) + BATCH_SIZE - 1) // BATCH_SIZE
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
            # 失败则降级为单票，仍限速
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
                    one = one.reset_index()
                    if "index" in one.columns and "time" not in one.columns:
                        one = one.rename(columns={"index": "time"})
                    if "code" not in one.columns:
                        one.insert(0, "code", code)
                    frames.append(one)
                    path = OUT_DIR / f"{code.replace('.', '_')}.csv"
                    one.to_csv(path, index=False, encoding="utf-8-sig")
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

        df = df.reset_index()
        if "index" in df.columns and "time" not in df.columns:
            df = df.rename(columns={"index": "time"})
        # panel=False 时长表通常已有 code 列
        if "code" not in df.columns:
            print(f"BATCH WARN {batch_no}: no code column, cols={list(df.columns)}")
            failed.extend(batch)
            continue

        frames.append(df)
        for code, g in df.groupby("code"):
            path = OUT_DIR / f"{str(code).replace('.', '_')}.csv"
            g.to_csv(path, index=False, encoding="utf-8-sig")

        got = df["code"].nunique()
        print(
            f"OK batch {batch_no}/{total_batches}: "
            f"stocks={got}/{len(batch)} rows={len(df)} secs={elapsed:.2f}"
        )

    if not frames:
        print("ERROR: no bars fetched", file=sys.stderr)
        return 3

    all_df = pd.concat(frames, ignore_index=True)
    all_path = OUT_DIR / "all_hs300_daily_1y.csv"
    all_df.to_csv(all_path, index=False, encoding="utf-8-sig")

    summary = (
        all_df.groupby("code")
        .agg(rows=("time", "count"), start=("time", "min"), end=("time", "max"))
        .reset_index()
    )
    summary_path = OUT_DIR / "summary.csv"
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    print(f"combined rows={len(all_df)} stocks={all_df['code'].nunique()} -> {all_path}")
    print(f"summary -> {summary_path}")
    if failed:
        fail_path = OUT_DIR / "failed.txt"
        fail_path.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"failed={len(failed)} -> {fail_path}")

    try:
        jq.logout()
    except Exception:
        pass
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
