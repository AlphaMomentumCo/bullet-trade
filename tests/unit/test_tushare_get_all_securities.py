import pandas as pd
import pytest

from bullet_trade.data.providers.tushare import TushareProvider


class DummyPro:
    def stock_basic(self, exchange="", list_status="L", fields=None):
        rows = {
            "L": {
                "ts_code": ["000001.SZ", "920000.BJ"],
                "name": ["平安银行", "北交所示例"],
                "list_date": ["19910403", "20211115"],
                "delist_date": [None, None],
                "market": ["主板", "北交所"],
                "list_status": ["L", "L"],
            },
            "D": {
                "ts_code": ["000004.SZ"],
                "name": ["国华网安"],
                "list_date": ["19901201"],
                "delist_date": ["20241231"],
                "market": ["主板"],
                "list_status": ["D"],
            },
            "P": {
                "ts_code": [],
                "name": [],
                "list_date": [],
                "delist_date": [],
                "market": [],
                "list_status": [],
            },
        }
        data = rows.get(list_status, rows["L"])
        return pd.DataFrame(data)

    def fund_basic(self, status="L", market="E", fields=None):
        return pd.DataFrame(
            {
                "ts_code": ["113000.SH"],
                "name": ["测试转债"],
                "found_date": [None],
                "delist_date": [None],
            }
        )

    def index_basic(self, market="SSE"):
        return pd.DataFrame(
            {
                "ts_code": ["000001.SH"],
                "fullname": ["测试指数"],
            }
        )


@pytest.mark.unit
def test_tushare_get_all_securities_handles_missing_dates(monkeypatch):
    provider = TushareProvider({"cache_dir": None, "include_bse": False})
    dummy = DummyPro()
    monkeypatch.setattr(provider, "_ensure_client", lambda: dummy)
    monkeypatch.setattr(provider, "_ch_available", lambda: False)
    monkeypatch.setattr(provider._cache, "cached_call", lambda name, kwargs, fn, result_type=None: fn(kwargs))

    df = provider.get_all_securities(types=["stock", "fund", "index"], date=None)

    assert not df.empty
    assert "start_date" in df.columns
    assert "end_date" in df.columns
    # date=None 仅 L，且默认剔除北交所
    assert "000001.XSHE" in df.index
    assert not any(str(i).endswith(".XBEI") for i in df.index)

    info = provider.get_security_info("000001.XSHE")
    assert info.get("display_name")


@pytest.mark.unit
def test_tushare_get_all_securities_date_includes_delisted_excludes_bse(monkeypatch):
    provider = TushareProvider({"cache_dir": None, "include_bse": False})
    dummy = DummyPro()
    monkeypatch.setattr(provider, "_ensure_client", lambda: dummy)
    monkeypatch.setattr(provider, "_ch_available", lambda: False)
    monkeypatch.setattr(provider._cache, "cached_call", lambda name, kwargs, fn, result_type=None: fn(kwargs))

    df = provider.get_all_securities(types="stock", date="2024-06-01")
    assert "000001.XSHE" in df.index
    assert "000004.XSHE" in df.index  # 当时仍在市的已退市股
    assert not any(str(i).endswith(".XBEI") for i in df.index)

    # 退市日之后不应再出现
    df2 = provider.get_all_securities(types="stock", date="2025-01-15")
    assert "000004.XSHE" not in df2.index


@pytest.mark.unit
def test_tushare_bse_code_mapping():
    assert TushareProvider._map_bse_numeric("821001") == "920001"
    assert TushareProvider._map_bse_numeric("830001") == "920001"
    assert TushareProvider._to_jq_code("821001.BJ") == "920001.XBEI"
    assert TushareProvider._is_bse_ts_code("920000.BJ")
    assert not TushareProvider._is_bse_ts_code("000001.SZ")
