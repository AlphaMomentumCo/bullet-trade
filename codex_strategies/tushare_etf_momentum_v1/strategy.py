from __future__ import annotations

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

    # ETF 交易成本比股票更低，使用更贴近真实的低费率配置。
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

    g.risk_index = "510300.XSHG"
    g.safe_asset = "511880.XSHG"
    g.offensive_universe = [
        "159915.XSHE",  # 创业板ETF
        "159949.XSHE",  # 创业板50ETF
        "512480.XSHG",  # 半导体ETF
        "512760.XSHG",  # 芯片ETF
        "159819.XSHE",  # 人工智能ETF
        "588000.XSHG",  # 科创50ETF
        "515790.XSHG",  # 光伏ETF
        "512000.XSHG",  # 券商ETF
    ]
    g.lookback = 120
    g.min_score = 0.012
    g.max_positions = 2
    g.risk_off_drawdown = 0.08
    g.cooldown_days = 8
    g.cooldown_until = None
    g.peak_total_value = context.portfolio.total_value
    g.last_rebalance_date = None

    run_weekly(rebalance, 1, time="open")
    run_daily(risk_control, time="close")


def _all_codes() -> List[str]:
    return list(dict.fromkeys([g.risk_index, g.safe_asset, *g.offensive_universe]))


def _fetch_close_frame(context, count: int) -> pd.DataFrame:
    df = get_price(
        _all_codes(),
        end_date=context.previous_date,
        count=count,
        frequency="daily",
        fields=["close"],
        fq="pre",
    )
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        if "close" not in set(df.columns.get_level_values(0)):
            return pd.DataFrame()
        close_block = df.xs("close", axis=1, level=0)
        close_block.columns = [str(col) for col in close_block.columns]
        return close_block.sort_index()

    if "close" in df.columns:
        # 单标的场景的兼容路径，尽量保持与多标的一致。
        code = _all_codes()[0]
        close_block = df[["close"]].copy()
        close_block.columns = [code]
        return close_block.sort_index()

    return pd.DataFrame()


def _series_for(close_frame: pd.DataFrame, code: str) -> pd.Series:
    if close_frame is None or close_frame.empty or code not in close_frame.columns:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(close_frame[code], errors="coerce").dropna()
    series.index = pd.to_datetime(series.index)
    return series.sort_index()


def _score_series(series: pd.Series) -> Optional[Dict[str, float]]:
    if series is None or series.empty or len(series) < g.lookback:
        return None

    window = series.tail(g.lookback).astype(float)
    if len(window) < g.lookback or window.iloc[-1] <= 0:
        return None

    def _safe_ret(n: int) -> float:
        if len(window) <= n:
            return 0.0
        base = float(window.iloc[-(n + 1)])
        if base <= 0:
            return 0.0
        return float(window.iloc[-1] / base - 1.0)

    ma20 = float(window.tail(20).mean()) if len(window) >= 20 else float(window.mean())
    ma60 = float(window.tail(60).mean()) if len(window) >= 60 else float(window.mean())
    ma120 = float(window.mean())
    ret20 = _safe_ret(20)
    ret60 = _safe_ret(60)
    ret120 = float(window.iloc[-1] / float(window.iloc[0]) - 1.0) if window.iloc[0] > 0 else 0.0
    trend20 = float(window.iloc[-1] / ma20 - 1.0) if ma20 > 0 else 0.0
    trend60 = float(window.iloc[-1] / ma60 - 1.0) if ma60 > 0 else 0.0
    trend120 = float(window.iloc[-1] / ma120 - 1.0) if ma120 > 0 else 0.0
    vol20 = float(window.pct_change().tail(20).std(ddof=0) * sqrt(250)) if len(window) > 1 else 0.0
    dd20 = float((window.tail(20) / window.tail(20).cummax() - 1.0).min()) if len(window) >= 2 else 0.0

    score = (
        0.38 * ret20
        + 0.27 * ret60
        + 0.15 * ret120
        + 0.10 * trend60
        + 0.10 * trend120
        - 0.12 * vol20
        + 0.08 * dd20
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
    }


def _risk_on(close_frame: pd.DataFrame) -> Tuple[bool, Dict[str, float]]:
    series = _series_for(close_frame, g.risk_index)
    if series.empty or len(series) < g.lookback:
        return False, {}

    window = series.tail(g.lookback).astype(float)
    ma60 = float(window.tail(60).mean()) if len(window) >= 60 else float(window.mean())
    ma120 = float(window.mean())
    latest = float(window.iloc[-1])
    ret20 = float(window.iloc[-1] / float(window.iloc[-21]) - 1.0) if len(window) > 20 and window.iloc[-21] > 0 else 0.0
    regime_score = 0.5 * (latest / ma60 - 1.0 if ma60 > 0 else 0.0) + 0.5 * (latest / ma120 - 1.0 if ma120 > 0 else 0.0)
    risk_on = latest > ma60 and latest > ma120 and ret20 > -0.01 and regime_score > 0.0
    return risk_on, {
        "latest": latest,
        "ma60": ma60,
        "ma120": ma120,
        "ret20": ret20,
        "regime_score": regime_score,
    }


def _select_targets(close_frame: pd.DataFrame) -> List[Tuple[str, float, Dict[str, float]]]:
    scored: List[Tuple[str, float, Dict[str, float]]] = []
    for code in g.offensive_universe:
        series = _series_for(close_frame, code)
        payload = _score_series(series)
        if not payload:
            continue
        if payload["score"] < g.min_score:
            continue
        scored.append((code, payload["score"], payload))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: g.max_positions]


def _rebalance_to_target(context, targets: Sequence[Tuple[str, float, Dict[str, float]]]) -> None:
    target_codes = {code for code, _, _ in targets}
    current_positions = list(context.portfolio.positions.keys())

    for stock in current_positions:
        if stock not in target_codes and stock != g.safe_asset:
            order_target_value(stock, 0)

    if not targets:
        order_target_value(g.safe_asset, context.portfolio.total_value * 0.98)
        return

    total_score = sum(score for _, score, _ in targets)
    if total_score <= 0:
        order_target_value(g.safe_asset, context.portfolio.total_value * 0.98)
        return

    for code, score, _payload in targets:
        weight = score / total_score
        order_target_value(code, context.portfolio.total_value * 0.98 * weight)

    if g.safe_asset in current_positions and g.safe_asset not in target_codes:
        order_target_value(g.safe_asset, 0)


def risk_control(context):
    current_value = float(context.portfolio.total_value)
    g.peak_total_value = max(g.peak_total_value, current_value)

    drawdown = 0.0
    if g.peak_total_value > 0:
        drawdown = 1.0 - current_value / g.peak_total_value

    today = context.current_dt.date()
    if drawdown >= g.risk_off_drawdown:
        g.cooldown_until = today + timedelta(days=g.cooldown_days)
        log.info(
            "[风险控制] 组合回撤触发熔断: peak=%.2f current=%.2f drawdown=%.2f%% cooldown_until=%s",
            g.peak_total_value,
            current_value,
            drawdown * 100.0,
            g.cooldown_until,
        )
        order_target_value(g.safe_asset, current_value * 0.98)
        for stock in list(context.portfolio.positions.keys()):
            if stock != g.safe_asset:
                order_target_value(stock, 0)


def rebalance(context):
    today = context.current_dt.date()
    if g.last_rebalance_date == today:
        return
    g.last_rebalance_date = today

    if g.cooldown_until is not None and today <= g.cooldown_until:
        order_target_value(g.safe_asset, context.portfolio.total_value * 0.98)
        for stock in list(context.portfolio.positions.keys()):
            if stock != g.safe_asset:
                order_target_value(stock, 0)
        log.info("[调仓] 冷静期内，继续持有防御资产: %s", g.safe_asset)
        return

    close_frame = _fetch_close_frame(context, g.lookback)
    if close_frame.empty:
        log.warn("[调仓] 未获取到行情，维持现有仓位")
        return

    risk_on, regime = _risk_on(close_frame)
    if not risk_on:
        g.cooldown_until = None
        _rebalance_to_target(context, [])
        log.info(
            "[调仓] 风险偏好关闭，切换到防御资产: latest=%.2f ma60=%.2f ma120=%.2f ret20=%.2f%%",
            regime.get("latest", 0.0),
            regime.get("ma60", 0.0),
            regime.get("ma120", 0.0),
            regime.get("ret20", 0.0) * 100.0,
        )
        return

    targets = _select_targets(close_frame)
    if not targets:
        _rebalance_to_target(context, [])
        log.info("[调仓] 无满足阈值的进攻标的，回到防御资产")
        return

    _rebalance_to_target(context, targets)
    log.info(
        "[调仓] 风险偏好开启，选中: %s",
        ", ".join([f"{code}:{score:.4f}" for code, score, _ in targets]),
    )
