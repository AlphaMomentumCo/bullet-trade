from __future__ import annotations

import glob
import json
import os
from datetime import timedelta
from math import sqrt
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from jqdata import *


def initialize(context):
    set_data_provider("tushare")
    set_benchmark("000300.XSHG")
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)

    set_slippage(FixedSlippage(0.0005))
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0,
            open_commission=0.0002,
            close_commission=0.0002,
            min_commission=5,
        ),
        type="stock",
    )

    g.benchmark_code = "000300.XSHG"
    g.universe = _load_cached_stock_universe()
    g.lookback = 130
    g.stage1_lookback = 21
    g.stage1_keep = 120
    g.top_n = 5
    g.min_strength = 0.006
    g.max_portfolio_drawdown = 0.090
    g.individual_stop_drawdown = 0.070
    g.individual_stop_gap = 0.040
    g.cooldown_days = 10
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    log.info("[初始化] 缓存股票池数量=%d", len(g.universe))

    run_weekly(rebalance, 1, time="9:30")
    run_daily(risk_control, time="14:50")


def _load_cached_stock_universe() -> List[str]:
    cache_dir = os.path.expanduser("~/.bullet-trade/cache/tushare/tushare/get_price")
    patterns = glob.glob(os.path.join(cache_dir, "*.meta.json"))
    stock_prefixes = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")

    universe = set()
    for path in patterns:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            continue
        sec = meta.get("params", {}).get("security")
        if not isinstance(sec, str):
            continue
        if sec == "000300.XSHG":
            continue
        if sec.startswith(stock_prefixes):
            universe.add(sec)

    return sorted(universe)


def _load_price_frame(stocks, end_date, count: int, fields: List[str]) -> pd.DataFrame:
    securities = [stocks] if isinstance(stocks, str) else list(stocks)
    frames: List[pd.DataFrame] = []

    for security in securities:
        try:
            df = get_price(
                security,
                end_date=end_date,
                count=count,
                frequency="daily",
                fields=fields,
                panel=False,
                fq=None,
            )
        except Exception:
            continue

        if df is None or df.empty:
            continue

        out = df.copy()
        if "code" not in out.columns:
            out["code"] = security
        else:
            out["code"] = out["code"].astype(str)
        for field in fields:
            if field in out.columns:
                out[field] = pd.to_numeric(out[field], errors="coerce")
        if not isinstance(out.index, pd.DatetimeIndex):
            continue
        frames.append(out)

    if not frames:
        return pd.DataFrame()

    merged = pd.concat(frames, axis=0, ignore_index=False)
    merged["code"] = merged["code"].astype(str)
    return merged.dropna(subset=[field for field in fields if field in merged.columns], how="all")


def _series_from_frame(frame: pd.DataFrame, code: str, field: str = "close") -> pd.Series:
    if frame is None or frame.empty:
        return pd.Series(dtype="float64")
    if "code" in frame.columns:
        sub = frame[frame["code"] == code]
        if sub.empty or field not in sub.columns:
            return pd.Series(dtype="float64")
        series = pd.to_numeric(sub[field], errors="coerce").dropna().sort_index()
        series.index = pd.to_datetime(series.index)
        return series
    if field not in frame.columns:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(frame[field], errors="coerce").dropna().sort_index()
    series.index = pd.to_datetime(series.index)
    return series


def _safe_ret(series: pd.Series, window: int) -> float:
    if len(series) <= window:
        return 0.0
    base = float(series.iloc[-(window + 1)])
    if base <= 0:
        return 0.0
    return float(series.iloc[-1] / base - 1.0)


def _stage1_filter(context, universe: Sequence[str]) -> List[str]:
    frame = _load_price_frame(universe, end_date=context.previous_date, count=g.stage1_lookback + 1, fields=["close"])
    if frame.empty:
        return []

    scored: List[Tuple[str, float]] = []
    for code in universe:
        series = _series_from_frame(frame, code, "close")
        closes = series.tail(g.stage1_lookback + 1)
        if len(closes) < g.stage1_lookback + 1 or closes.iloc[0] <= 0:
            continue
        ret20 = float(closes.iloc[-1] / closes.iloc[0] - 1.0)
        if pd.isna(ret20):
            continue
        scored.append((code, ret20))

    scored.sort(key=lambda item: item[1], reverse=True)
    picked = [code for code, score in scored[: g.stage1_keep] if score > -0.08]
    log.info(
        "[选池] 缓存候选=%d, 有效=%d, 进入二阶段=%d, 前几名=%s",
        len(universe),
        len(scored),
        len(picked),
        ", ".join([f"{code}:{score:.4f}" for code, score in scored[: min(5, len(scored))]]),
    )
    return picked


def _score_series(series: pd.Series, benchmark: Optional[pd.Series] = None) -> Optional[Dict[str, float]]:
    if series is None:
        return None
    closes = pd.to_numeric(series, errors="coerce").dropna().tail(g.lookback + 1)
    if len(closes) < 61 or closes.iloc[0] <= 0:
        return None

    recent = closes.tail(min(g.lookback, len(closes)))
    ma20 = float(recent.tail(20).mean()) if len(recent) >= 20 else float(recent.mean())
    ma60 = float(recent.tail(60).mean()) if len(recent) >= 60 else float(recent.mean())
    ma120 = float(recent.tail(120).mean()) if len(recent) >= 120 else float(recent.mean())

    ret5 = _safe_ret(closes, 5)
    ret20 = _safe_ret(closes, 20)
    ret60 = _safe_ret(closes, 60)
    ret120 = _safe_ret(closes, 120)
    trend20 = float(recent.iloc[-1] / ma20 - 1.0) if ma20 > 0 else 0.0
    trend60 = float(recent.iloc[-1] / ma60 - 1.0) if ma60 > 0 else 0.0
    trend120 = float(recent.iloc[-1] / ma120 - 1.0) if ma120 > 0 else 0.0
    vol20 = float(recent.pct_change().tail(20).std(ddof=0) * sqrt(252)) if len(recent) > 1 else 0.0
    dd20 = float((recent.tail(20) / recent.tail(20).cummax() - 1.0).min()) if len(recent) > 1 else 0.0

    rel60 = 0.0
    if benchmark is not None and not benchmark.empty:
        bench = pd.to_numeric(benchmark, errors="coerce").dropna().tail(g.lookback + 1)
        if len(bench) >= 61 and bench.iloc[0] > 0:
            rel60 = ret60 - _safe_ret(bench, 60)

    score = (
        0.08 * ret5
        + 0.18 * ret20
        + 0.24 * ret60
        + 0.12 * ret120
        + 0.08 * trend20
        + 0.08 * trend60
        + 0.08 * trend120
        + 0.08 * rel60
        - 0.05 * vol20
        + 0.04 * dd20
    )

    return {
        "score": float(score),
        "ret20": float(ret20),
        "ret60": float(ret60),
        "ret120": float(ret120),
        "trend20": float(trend20),
        "trend60": float(trend60),
        "trend120": float(trend120),
        "vol20": float(vol20),
        "dd20": float(dd20),
        "rel60": float(rel60),
    }


def _move_to_cash(context, reason: str) -> None:
    for stock in list(context.portfolio.positions.keys()):
        order_target_value(stock, 0)
    log.info("[调仓] %s，全部切换为现金", reason)


def _select_targets(context) -> List[Tuple[str, Dict[str, float]]]:
    if not g.universe:
        return []

    stage1 = _stage1_filter(context, g.universe)
    if not stage1:
        return []

    frame = _load_price_frame([*stage1, g.benchmark_code], end_date=context.previous_date, count=g.lookback + 1, fields=["close"])
    if frame.empty:
        return []

    benchmark = _series_from_frame(frame, g.benchmark_code, "close")
    ranked: List[Tuple[str, Dict[str, float]]] = []
    for code in stage1:
        series = _series_from_frame(frame, code, "close")
        payload = _score_series(series, benchmark=benchmark)
        if not payload:
            continue
        ranked.append((code, payload))

    ranked.sort(key=lambda item: item[1]["score"], reverse=True)
    positive = [(code, payload) for code, payload in ranked if payload["score"] > 0]
    if positive:
        log.info(
            "[选股] 有效=%d, 正分=%d, 前几名=%s",
            len(ranked),
            len(positive),
            ", ".join([f"{code}:{payload['score']:.4f}" for code, payload in positive[: min(5, len(positive))]]),
        )
    else:
        log.info("[选股] 无正分标的")
    return positive[: g.top_n]


def risk_control(context):
    current_value = float(context.portfolio.total_value)
    g.peak_total_value = max(g.peak_total_value, current_value)
    if g.peak_total_value <= 0:
        return

    drawdown = 1.0 - current_value / g.peak_total_value
    today = context.current_dt.date()
    if drawdown >= g.max_portfolio_drawdown:
        g.cooldown_until = today + timedelta(days=g.cooldown_days)
        log.info(
            "[风控] 组合回撤触发熔断: peak=%.2f current=%.2f drawdown=%.2f%% cooldown_until=%s",
            g.peak_total_value,
            current_value,
            drawdown * 100.0,
            g.cooldown_until,
        )
        _move_to_cash(context, "组合回撤触发熔断")
        return

    for stock in list(context.portfolio.positions.keys()):
        frame = _load_price_frame(stock, end_date=context.previous_date, count=21, fields=["close"])
        series = _series_from_frame(frame, stock, "close")
        if len(series) < 21:
            continue
        closes = series.tail(21)
        latest = float(closes.iloc[-1])
        peak_20 = float(closes.max())
        ma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else float(closes.mean())
        trailing_dd = 1.0 - latest / peak_20 if peak_20 > 0 else 0.0
        if trailing_dd >= g.individual_stop_drawdown or (ma20 > 0 and latest < ma20 * (1.0 - g.individual_stop_gap)):
            log.info(
                "[风控] 个股止损: %s latest=%.2f peak20=%.2f ma20=%.2f dd=%.2f%%",
                stock,
                latest,
                peak_20,
                ma20,
                trailing_dd * 100.0,
            )
            order_target_value(stock, 0)


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    if g.cooldown_until is not None and today <= g.cooldown_until:
        _move_to_cash(context, "冷静期内")
        return

    targets = _select_targets(context)
    if not targets:
        _move_to_cash(context, "没有满足阈值的强势标的")
        return

    scores = [payload["score"] for _, payload in targets]
    strength = sum(scores) / len(scores)
    if strength < g.min_strength:
        _move_to_cash(context, f"强度不足 strength={strength:.4f}")
        return

    exposure = min(1.0, max(0.30, (strength - g.min_strength) / 0.025))
    total_score = sum(max(payload["score"], 0.0) for _, payload in targets)
    if total_score <= 0:
        _move_to_cash(context, "评分总和为零")
        return

    target_weights: Dict[str, float] = {}
    for code, payload in targets:
        target_weights[code] = exposure * max(payload["score"], 0.0) / total_score

    for stock in list(context.portfolio.positions.keys()):
        if stock not in target_weights:
            order_target_value(stock, 0)

    current_data = get_current_data()
    tradable = {
        code: weight
        for code, weight in target_weights.items()
        if code in current_data and not current_data[code].paused
    }
    if not tradable:
        _move_to_cash(context, "目标标的停牌或不可交易")
        return

    total_weight = sum(tradable.values())
    if total_weight <= 0:
        _move_to_cash(context, "目标权重为零")
        return

    for code, weight in tradable.items():
        order_target_value(code, context.portfolio.total_value * 0.98 * weight / total_weight)

    log.info(
        "[调仓] strength=%.4f exposure=%.2f 选中: %s",
        strength,
        exposure,
        ", ".join([f"{code}:{payload['score']:.4f}" for code, payload in targets]),
    )
