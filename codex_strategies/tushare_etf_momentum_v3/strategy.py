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

    # 更宽的、跨风格的 ETF 池，避免只盯着少数热点主题造成过拟合。
    g.universe = [
        "510050.XSHG",  # 上证50ETF
        "510300.XSHG",  # 沪深300ETF
        "510500.XSHG",  # 中证500ETF
        "159915.XSHE",  # 创业板ETF
        "159949.XSHE",  # 创业板50ETF
        "588000.XSHG",  # 科创50ETF
        "512480.XSHG",  # 半导体ETF
        "512760.XSHG",  # 芯片ETF
        "159819.XSHE",  # 人工智能ETF
        "515790.XSHG",  # 光伏ETF
        "512000.XSHG",  # 券商ETF
        "512170.XSHG",  # 医疗ETF
        "512690.XSHG",  # 酒ETF
        "512980.XSHG",  # 银行ETF
        "512710.XSHG",  # 军工ETF
        "516160.XSHG",  # 新能源车ETF
        "511010.XSHG",  # 国债ETF
        "511880.XSHG",  # 货币ETF
        "518880.XSHG",  # 黄金ETF
    ]
    g.lookback = 60
    g.max_positions = 1
    g.min_score = 0.0
    g.peak_total_value = context.portfolio.total_value
    g.cooldown_until = None
    g.risk_off_drawdown = 0.07
    g.cooldown_days = 5

    run_weekly(rebalance, 1, time="9:35")
    run_daily(risk_control, time="14:50")


def _price_frame(context) -> pd.DataFrame:
    df = get_price(
        g.universe,
        end_date=context.previous_date,
        count=g.lookback,
        frequency="daily",
        fields=["close"],
        fq="pre",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        if "close" not in set(df.columns.get_level_values(0)):
            return pd.DataFrame()
        close_frame = df.xs("close", axis=1, level=0)
        close_frame.columns = [str(c) for c in close_frame.columns]
        return close_frame.sort_index()
    if "close" in df.columns:
        close_frame = df[["close"]].copy()
        close_frame.columns = [g.universe[0]]
        return close_frame.sort_index()
    return pd.DataFrame()


def _series(close_frame: pd.DataFrame, code: str) -> pd.Series:
    if close_frame.empty or code not in close_frame.columns:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(close_frame[code], errors="coerce").dropna()
    return series.sort_index()


def _is_listed_on(code: str, current_date) -> bool:
    info = None
    try:
        info = get_security_info(code, date=current_date)
    except Exception:
        return False
    if not info:
        return False
    start_date = getattr(info, "start_date", None)
    if start_date is None and isinstance(info, dict):
        start_date = info.get("start_date")
    if start_date is None:
        return True
    try:
        return pd.Timestamp(start_date).date() <= current_date
    except Exception:
        return True


def _score(series: pd.Series) -> Optional[Dict[str, float]]:
    if series is None or series.empty or len(series) < g.lookback:
        return None
    w = series.tail(g.lookback).astype(float)
    if w.iloc[-1] <= 0:
        return None

    def ret(n: int) -> float:
        if len(w) <= n or w.iloc[-(n + 1)] <= 0:
            return 0.0
        return float(w.iloc[-1] / w.iloc[-(n + 1)] - 1.0)

    ma20 = float(w.tail(20).mean())
    trend20 = float(w.iloc[-1] / ma20 - 1.0) if ma20 > 0 else 0.0
    vol20 = float(w.pct_change().tail(20).std(ddof=0) * sqrt(250)) if len(w) > 1 else 0.0
    dd20 = float((w.tail(20) / w.tail(20).cummax() - 1.0).min()) if len(w) >= 2 else 0.0

    score = (
        0.18 * ret(5)
        + 0.24 * ret(10)
        + 0.30 * ret(20)
        + 0.18 * ret(40)
        + 0.10 * trend20
        - 0.12 * vol20
        + 0.06 * dd20
    )
    return {
        "score": float(score),
        "ret5": float(ret(5)),
        "ret10": float(ret(10)),
        "ret20": float(ret(20)),
        "ret40": float(ret(40)),
        "trend20": float(trend20),
        "vol20": float(vol20),
        "dd20": float(dd20),
    }


def _select_targets(close_frame: pd.DataFrame) -> List[Tuple[str, float, Dict[str, float]]]:
    ranked: List[Tuple[str, float, Dict[str, float]]] = []
    current_date = pd.Timestamp(close_frame.index.max()).date()
    for code in g.universe:
        if not _is_listed_on(code, current_date):
            continue
        payload = _score(_series(close_frame, code))
        if not payload or payload["score"] <= g.min_score:
            continue
        ranked.append((code, payload["score"], payload))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[: g.max_positions]


def _rebalance(context, targets: Sequence[Tuple[str, float, Dict[str, float]]]) -> None:
    target_codes = {code for code, _, _ in targets}
    for stock in list(context.portfolio.positions.keys()):
        if stock not in target_codes:
            order_target_value(stock, 0)

    if not targets:
        return

    total_score = sum(score for _, score, _ in targets)
    if total_score <= 0:
        return

    for code, score, _payload in targets:
        weight = score / total_score
        order_target_value(code, context.portfolio.total_value * 0.98 * weight)


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
            "[风险控制] 回撤熔断: peak=%.2f current=%.2f drawdown=%.2f%% cooldown_until=%s",
            g.peak_total_value,
            current_value,
            drawdown * 100.0,
            g.cooldown_until,
        )
        for stock in list(context.portfolio.positions.keys()):
            order_target_value(stock, 0)


def rebalance(context):
    today = context.current_dt.date()
    if g.cooldown_until is not None and today <= g.cooldown_until:
        for stock in list(context.portfolio.positions.keys()):
            order_target_value(stock, 0)
        log.info("[调仓] 冷静期内保持空仓")
        return

    close_frame = _price_frame(context)
    if close_frame.empty:
        log.warning("[调仓] 未获取到价格数据")
        return

    targets = _select_targets(close_frame)
    if not targets:
        for stock in list(context.portfolio.positions.keys()):
            order_target_value(stock, 0)
        log.info("[调仓] 没有满足阈值的强势标的，空仓")
        return

    _rebalance(context, targets)
    log.info(
        "[调仓] 选中: %s",
        ", ".join([f"{code}:{score:.4f}" for code, score, _ in targets]),
    )
