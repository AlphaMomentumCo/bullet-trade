from datetime import datetime

import pandas as pd
import pytest

from bullet_trade.data.providers.tushare import TushareProvider


@pytest.mark.unit
def test_tushare_prefactor_uses_latest_trade_day(monkeypatch):
    provider = TushareProvider({"cache_dir": None})
    monkeypatch.setattr(provider, "_ch_available", lambda: False)

    df = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.0],
            "low": [10.0],
            "close": [10.0],
            "volume": [1000.0],
        },
        index=pd.to_datetime(["2025-07-01"]),
    )

    calls = []

    def fake_fetch_adj_factor(security, start_dt, end_dt):
        start_d = pd.to_datetime(start_dt).date()
        end_d = pd.to_datetime(end_dt).date()
        calls.append((start_d, end_d))
        rows = []
        d1 = datetime(2025, 7, 1).date()
        d2 = datetime(2025, 7, 2).date()
        if start_d <= d1 <= end_d:
            rows.append({"trade_date": "20250701", "adj_factor": 1.0})
        if start_d <= d2 <= end_d:
            rows.append({"trade_date": "20250702", "adj_factor": 0.98})
        return pd.DataFrame(rows)

    monkeypatch.setattr(provider, "_fetch_adj_factor", fake_fetch_adj_factor)
    monkeypatch.setattr(provider, "get_trade_days", lambda start_date=None, end_date=None, count=None: [datetime(2025, 7, 2)])

    adjusted = provider._apply_adjustment("000001.XSHE", df, "pre", pre_factor_ref_date=None)

    assert adjusted.loc[pd.Timestamp("2025-07-01"), "close"] != df.loc[pd.Timestamp("2025-07-01"), "close"]
    # 应拉到最新交易日作为前复权锚点
    assert any(end_d >= datetime(2025, 7, 2).date() for _, end_d in calls)
    # 成交量按价格比例反权重
    assert adjusted.loc[pd.Timestamp("2025-07-01"), "volume"] != df.loc[pd.Timestamp("2025-07-01"), "volume"]
