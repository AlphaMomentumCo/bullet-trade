"""一次性拉取沪深300 / 中证1000 近一年日K（按 jqdatasdk QPS 限速）。"""
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
OUT_DIR = Path(__file__).resolve().parent

# 官方 FAQ：jqdatasdk 约 30 次/秒（部分说明为单接口 100 次/秒）。
# 此处保守限速约 2 QPS，远低于上限。
MIN_INTERVAL_SEC = 0.5

SYMBOLS = [
    ("000300.XSHG", "沪深300"),
    ("000852.XSHG", "中证1000"),
]
FIELDS = ["open", "close", "high", "low", "volume", "money", "pre_close", "paused"]


def main() -> int:
    load_dotenv(ROOT / ".env")
    user = os.getenv("JQDATA_USERNAME") or os.getenv("JQDATA_USER")
    pwd = os.getenv("JQDATA_PASSWORD") or os.getenv("JQDATA_PWD")
    if not user or not pwd:
        print("ERROR: .env 缺少 JQDATA_USERNAME / JQDATA_PASSWORD", file=sys.stderr)
        return 1

    # 试用/受限账号常见窗口约一年；优先用权限内近一年，避免越界报错。
    # 若账号权限提示具体起止日，可被环境变量覆盖：
    #   JQDATA_PULL_START / JQDATA_PULL_END
    end_env = os.getenv("JQDATA_PULL_END")
    start_env = os.getenv("JQDATA_PULL_START")
    if end_env and start_env:
        end = datetime.strptime(end_env, "%Y-%m-%d").date()
        start = datetime.strptime(start_env, "%Y-%m-%d").date()
    else:
        # 当前账号反馈可用至约 2026-05-01，取此前完整一年
        end = datetime(2026, 5, 1).date()
        start = end - timedelta(days=365)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"auth user={user[:3]}***")
    jq.auth(user, pwd)
    print("auth ok")
    print(f"range: {start} -> {end}")
    print(f"qps guard: min interval {MIN_INTERVAL_SEC}s")

    last_call = 0.0
    frames = []

    for code, name in SYMBOLS:
        wait = MIN_INTERVAL_SEC - (time.monotonic() - last_call)
        if wait > 0:
            time.sleep(wait)

        t0 = time.monotonic()
        df = jq.get_price(
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
        elapsed = last_call - t0

        if df is None or len(df) == 0:
            print(f"WARN empty: {name} {code}")
            continue

        df = df.reset_index()
        if "index" in df.columns and "time" not in df.columns:
            df = df.rename(columns={"index": "time"})
        if "time" not in df.columns:
            for col in list(df.columns):
                cl = str(col).lower()
                if cl in ("date", "datetime") or "time" in cl:
                    df = df.rename(columns={col: "time"})
                    break

        df.insert(0, "name", name)
        df.insert(0, "code", code)

        path = OUT_DIR / f"{code.replace('.', '_')}_daily_1y.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        frames.append(df)
        print(f"OK {name} {code}: rows={len(df)} secs={elapsed:.2f} -> {path}")

    if not frames:
        print("ERROR: no data fetched", file=sys.stderr)
        return 2

    all_df = pd.concat(frames, ignore_index=True)
    all_path = OUT_DIR / "hs300_zz1000_daily_1y.csv"
    all_df.to_csv(all_path, index=False, encoding="utf-8-sig")
    print(f"combined rows={len(all_df)} -> {all_path}")
    print(all_df.groupby(["code", "name"]).size().to_string())
    if "time" in all_df.columns:
        print("date span:")
        print(all_df.groupby("code")["time"].agg(["min", "max"]).to_string())

    try:
        jq.logout()
    except Exception:
        pass
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
