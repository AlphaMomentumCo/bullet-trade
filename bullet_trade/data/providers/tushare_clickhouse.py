"""
TushareProvider 的 ClickHouse 热链路读库客户端（读路径优化版）。

存储/表设计要点（schema v5）:
1. 源表 ReplacingMergeTree 读默认不加 FINAL；投影到 bt_*_fast（MergeTree）
2. 窄列 + LowCardinality + ORDER BY (ts_code, trade_date)；日线 PARTITION BY toYYYYMM
3. bt_daily_adj_fast：日线+复权宽表，热路径消 JOIN
4. PREWHERE：ts_code + 日期区间；热列裁剪；Date 直出（避免 YYYYMMDD 往返）
5. 开市日窄表 bt_trade_cal_open_fast；daily_basic / index_weight 支持 asof

物化权威实现已迁至 ``tushare-integration``（分支 dev/table_delay_opt_0905）：
``python main.py fast materialize``。本模块默认 ``auto_ensure_fast=False``，策略侧只读。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# 源表名（外部 ETL / tushare-integration 等写入的 ClickHouse 表）
TABLE_DAILY = "daily"
TABLE_ADJ_FACTOR = "adj_factor"
TABLE_INDEX_DAILY = "index_daily"
TABLE_INDEX_DAILY_CORE = "index_daily_core"
TABLE_FUND_DAILY = "fund_daily"
TABLE_TRADE_CAL = "trade_cal"
TABLE_STOCK_BASIC = "stock_basic"
TABLE_DAILY_BASIC = "daily_basic"
TABLE_FUND_BASIC = "fund_basic"
TABLE_INDEX_WEIGHT = "index_weight"

# 读优化表（本模块 ensure_fast_tables 创建）
FAST_SCHEMA_VERSION = 5
FAST_META = "bt_fast_meta"
FAST_DAILY = "bt_daily_fast"
FAST_DAILY_ADJ = "bt_daily_adj_fast"  # 日线+复权宽表（热路径首选）
FAST_ADJ = "bt_adj_factor_fast"
FAST_TRADE_CAL = "bt_trade_cal_fast"
FAST_TRADE_CAL_OPEN = "bt_trade_cal_open_fast"
FAST_STOCK_BASIC = "bt_stock_basic_fast"
FAST_DICT_STOCK_BASIC = "bt_stock_basic_dict"
FAST_DAILY_BASIC = "bt_daily_basic_fast"
FAST_FUND_BASIC = "bt_fund_basic_fast"
FAST_INDEX_WEIGHT = "bt_index_weight_fast"
FAST_INDEX_DAILY = "bt_index_daily_fast"

# get_price 热路径默认列（不含 pre_close/change/pct_chg）
_HOT_OHLCV_COLS = ("open", "high", "low", "close", "vol", "amount")
_HOT_OHLCV_ADJ_COLS = _HOT_OHLCV_COLS + ("adj_factor",)


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _ymd_to_dash(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return pd.to_datetime(text).strftime("%Y-%m-%d")
    except Exception:
        return text


def _escape_sql_str(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "''")


def _load_yaml_database(path: str) -> Dict[str, Any]:
    cfg_path = Path(os.path.expanduser(path))
    if not cfg_path.is_file():
        return {}
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        db = raw.get("database") or {}
        return db if isinstance(db, dict) else {}
    except Exception as exc:
        logger.debug("读取 ClickHouse 配置失败 %s: %s", cfg_path, exc)
        return {}


class TushareClickHouseClient:
    """轻量 ClickHouse 查询封装；线程本地复用连接。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = dict(config or {})
        yaml_path = (
            cfg.get("config_path")
            or os.getenv("TUSHARE_CLICKHOUSE_CONFIG")
            or ""
        )
        yaml_db = _load_yaml_database(str(yaml_path)) if yaml_path else {}

        enabled_raw = cfg.get("enabled")
        if enabled_raw is None:
            enabled_raw = os.getenv("TUSHARE_CLICKHOUSE")
        default_enabled = bool(yaml_db) or bool(
            cfg.get("host") or os.getenv("TUSHARE_CLICKHOUSE_HOST")
        )
        self.enabled = _parse_bool(enabled_raw, default=default_enabled)

        self.host = str(
            cfg.get("host")
            or os.getenv("TUSHARE_CLICKHOUSE_HOST")
            or yaml_db.get("host")
            or "127.0.0.1"
        )
        # 默认走 HTTP 8123；可设 TUSHARE_CLICKHOUSE_PORT=9000 尝试 native
        self.port = int(
            cfg.get("port")
            or os.getenv("TUSHARE_CLICKHOUSE_PORT")
            or yaml_db.get("port")
            or 8123
        )
        self.user = str(
            cfg.get("user")
            or os.getenv("TUSHARE_CLICKHOUSE_USER")
            or yaml_db.get("user")
            or "default"
        )
        self.password = str(
            cfg.get("password")
            if cfg.get("password") is not None
            else (
                os.getenv("TUSHARE_CLICKHOUSE_PASSWORD")
                if os.getenv("TUSHARE_CLICKHOUSE_PASSWORD") is not None
                else yaml_db.get("password", "")
            )
        )
        self.database = str(
            cfg.get("db_name")
            or cfg.get("database")
            or os.getenv("TUSHARE_CLICKHOUSE_DB")
            or yaml_db.get("db_name")
            or "default"
        )
        # 读优化：默认关闭 FINAL（大表 FINAL 是热读变慢主因）
        self.use_final = _parse_bool(
            cfg.get("use_final", os.getenv("TUSHARE_CLICKHOUSE_FINAL", "0")),
            default=False,
        )
        self.prefer_fast = _parse_bool(
            cfg.get("prefer_fast", os.getenv("TUSHARE_CLICKHOUSE_FAST", "1")),
            default=True,
        )
        # 默认关闭：物化已迁至 tushare-integration（python main.py fast materialize）
        self.auto_ensure_fast = _parse_bool(
            cfg.get("auto_ensure_fast", os.getenv("TUSHARE_CLICKHOUSE_AUTO_FAST", "0")),
            default=False,
        )

        self._local = threading.local()
        self._known_tables: Set[str] = set()
        self._missing_tables: Set[str] = set()
        self._available: Optional[bool] = None
        self._fast_ready = False

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        if self._available is not None:
            return bool(self._available)
        try:
            client = self._client()
            client.query("SELECT 1")
            self._available = True
            logger.info(
                "Tushare ClickHouse 热链路已连接 %s:%s/%s (final=%s fast=%s)",
                self.host,
                self.port,
                self.database,
                self.use_final,
                self.prefer_fast,
            )
            if self.auto_ensure_fast and self.prefer_fast:
                try:
                    self.ensure_fast_tables(optimize=False)
                except Exception as exc:
                    logger.debug("自动创建 fast 表跳过: %s", exc)
        except Exception as exc:
            self._available = False
            logger.warning("Tushare ClickHouse 不可用，将回退远程 API: %s", exc)
        return bool(self._available)

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            import clickhouse_connect

            kwargs: Dict[str, Any] = dict(
                host=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                database=self.database,
                compress=True,
            )
            # 9000 端口优先 native
            if int(self.port) == 9000:
                kwargs["interface"] = "native"
            client = clickhouse_connect.get_client(**kwargs)
            self._local.client = client
        return client

    def query_df(self, sql: str) -> pd.DataFrame:
        if not self.is_available():
            return pd.DataFrame()
        try:
            df = self._client().query_df(sql)
            return df if df is not None else pd.DataFrame()
        except Exception as exc:
            if self.use_final and " FINAL" in sql.upper():
                try:
                    sql2 = sql.replace(" FINAL", "").replace(" final", "")
                    df = self._client().query_df(sql2)
                    return df if df is not None else pd.DataFrame()
                except Exception as exc2:
                    logger.debug("ClickHouse 查询失败(无FINAL重试): %s | sql=%s", exc2, sql[:240])
                    return pd.DataFrame()
            logger.debug("ClickHouse 查询失败: %s | sql=%s", exc, sql[:240])
            return pd.DataFrame()

    def query_columns(self, sql: str) -> pd.DataFrame:
        """列式组装 DataFrame，热路径批量查询避免 query_df 行式组装开销。"""
        if not self.is_available():
            return pd.DataFrame()
        try:
            result = self._client().query(sql)
            names = list(result.column_names or [])
            cols = list(result.result_columns or [])
            if not names:
                return pd.DataFrame()
            data = {names[i]: cols[i] for i in range(len(names))}
            return pd.DataFrame(data)
        except Exception as exc:
            logger.debug("ClickHouse query_columns 失败: %s | sql=%s", exc, sql[:240])
            # 回退 query_df
            return self.query_df(sql)

    def query_one(self, sql: str) -> Optional[Dict[str, Any]]:
        """点查：走 query 结果行，避免 query_df 的 pandas 组装开销。"""
        if not self.is_available():
            return None
        try:
            result = self._client().query(sql)
            if not result.result_rows:
                return None
            cols = list(result.column_names)
            row = result.result_rows[0]
            return {cols[i]: row[i] for i in range(len(cols))}
        except Exception as exc:
            logger.debug("ClickHouse query_one 失败: %s | sql=%s", exc, sql[:240])
            return None

    def command(self, sql: str) -> None:
        if not self.is_available():
            return
        self._client().command(sql)

    def table_exists(self, table: str) -> bool:
        if not self.is_available():
            return False
        if table in self._known_tables:
            return True
        if table in self._missing_tables:
            return False
        df = self.query_df(
            f"SELECT count() AS c FROM system.tables "
            f"WHERE database = '{_escape_sql_str(self.database)}' "
            f"AND name = '{_escape_sql_str(table)}'"
        )
        exists = False
        if df is not None and not df.empty:
            try:
                exists = int(df.iloc[0]["c"] if "c" in df.columns else df.iloc[0, 0]) > 0
            except Exception:
                exists = False
        if exists:
            self._known_tables.add(table)
        else:
            self._missing_tables.add(table)
        return exists

    def _resolve_table(self, fast_name: str, fallback_names: Sequence[str]) -> Optional[str]:
        if self.prefer_fast and self.table_exists(fast_name):
            return fast_name
        for name in fallback_names:
            if self.table_exists(name):
                return name
        return None

    def _from_clause(self, table: str) -> str:
        base = f"`{_escape_sql_str(table)}`"
        # fast 表是 MergeTree 无重复，永不加 FINAL
        if table.startswith("bt_") and table.endswith("_fast"):
            return base
        return f"{base} FINAL" if self.use_final else base

    @staticmethod
    def _date_ymd_expr(col: str, alias: Optional[str] = None) -> str:
        # 兼容旧调用：仍可用 UInt32 YYYYMMDD；热路径优先直接选 Date 列
        out = alias or f"{col}_ymd"
        return f"toYYYYMMDD(toDate({col})) AS {out}"

    @staticmethod
    def _normalize_trade_date_col(df: pd.DataFrame, col: str = "trade_date") -> pd.DataFrame:
        """统一 trade_date 为可比较的日期列；兼容 Date / YYYYMMDD / 字符串。"""
        if df is None or df.empty:
            return df if df is not None else pd.DataFrame()
        ymd = f"{col}_ymd"
        out = df
        if ymd in out.columns and col not in out.columns:
            out = out.rename(columns={ymd: col})
        if col not in out.columns:
            return out
        series = out[col]
        # 已是 datetime64 / date
        if pd.api.types.is_datetime64_any_dtype(series):
            return out
        # UInt32 / int YYYYMMDD
        if pd.api.types.is_integer_dtype(series) or (
            hasattr(series.dtype, "name") and str(series.dtype).startswith("UInt")
        ):
            try:
                out = out.copy()
                out[col] = pd.to_datetime(series.astype("Int64").astype(str).str.zfill(8), format="%Y%m%d", errors="coerce")
                return out
            except Exception:
                pass
        out = out.copy()
        out[col] = pd.to_datetime(series, errors="coerce")
        return out

    @staticmethod
    def _rename_ymd(df: pd.DataFrame, col: str = "trade_date") -> pd.DataFrame:
        return TushareClickHouseClient._normalize_trade_date_col(df, col=col)

    @staticmethod
    def _date_prewhere_sql(
        start: Optional[str],
        end: Optional[str],
        *,
        col: str = "trade_date",
    ) -> List[str]:
        parts: List[str] = []
        if start:
            parts.append(f"{col} >= toDate('{start}')")
        if end:
            parts.append(f"{col} <= toDate('{end}')")
        return parts

    def _fast_schema_version(self) -> Optional[int]:
        if not self.table_exists(FAST_META):
            return None
        row = self.query_one(f"SELECT version FROM `{FAST_META}` LIMIT 1")
        if not row:
            return None
        try:
            return int(row.get("version"))
        except Exception:
            return None

    def _write_fast_schema_version(self) -> None:
        self.command(
            f"""
            CREATE TABLE IF NOT EXISTS `{FAST_META}`
            (
              version UInt32,
              updated_at DateTime DEFAULT now()
            )
            ENGINE = MergeTree
            ORDER BY version
            """
        )
        self.command(f"TRUNCATE TABLE IF EXISTS `{FAST_META}`")
        self.command(
            f"INSERT INTO `{FAST_META}` (version) VALUES ({int(FAST_SCHEMA_VERSION)})"
        )
        self._known_tables.add(FAST_META)
        self._missing_tables.discard(FAST_META)

    # -------------------- 读优化表 --------------------
    def ensure_fast_tables(self, *, optimize: bool = True, force: bool = False) -> Dict[str, int]:
        """
        从源表投影到读优化 MergeTree（窄列 + 正确 ORDER BY + 合适粒度）。

        - schema version 变化时自动 force 重建
        - force=True：DROP 后全量重灌
        """
        if not self.is_available():
            return {}
        ver = self._fast_schema_version()
        if ver != FAST_SCHEMA_VERSION:
            force = True
            logger.info(
                "fast schema %s -> %s，重建读优化表", ver, FAST_SCHEMA_VERSION
            )
        if force:
            try:
                self.command(f"DROP DICTIONARY IF EXISTS `{FAST_DICT_STOCK_BASIC}`")
            except Exception:
                pass

        created: Dict[str, int] = {}
        # finer granularity for point / small-dimension tables
        # 日线类：PARTITION BY toYYYYMM + ORDER BY (ts_code, trade_date) + minmax(trade_date)
        _daily_engine = """
                ENGINE = MergeTree
                PARTITION BY toYYYYMM(trade_date)
                ORDER BY (ts_code, trade_date)
                SETTINGS index_granularity = 4096
                """
        specs: List[Tuple[str, str, str, str]] = [
            (
                FAST_DAILY,
                TABLE_DAILY,
                f"""
                (
                  ts_code LowCardinality(String),
                  trade_date Date,
                  open Float64, high Float64, low Float64, close Float64,
                  pre_close Float64, change Float64, pct_chg Float64,
                  vol Float64, amount Float64,
                  INDEX idx_td trade_date TYPE minmax GRANULARITY 1
                )
                {_daily_engine}
                """,
                f"""
                SELECT
                  ts_code, toDate(trade_date) AS trade_date,
                  toFloat64OrZero(toString(open)) AS open,
                  toFloat64OrZero(toString(high)) AS high,
                  toFloat64OrZero(toString(low)) AS low,
                  toFloat64OrZero(toString(close)) AS close,
                  toFloat64OrZero(toString(pre_close)) AS pre_close,
                  toFloat64OrZero(toString(change)) AS change,
                  toFloat64OrZero(toString(pct_chg)) AS pct_chg,
                  toFloat64OrZero(toString(vol)) AS vol,
                  toFloat64OrZero(toString(amount)) AS amount
                FROM {TABLE_DAILY}
                """,
            ),
            (
                FAST_DAILY_ADJ,
                TABLE_DAILY,
                f"""
                (
                  ts_code LowCardinality(String),
                  trade_date Date,
                  open Float64, high Float64, low Float64, close Float64,
                  vol Float64, amount Float64,
                  adj_factor Float64,
                  INDEX idx_td trade_date TYPE minmax GRANULARITY 1
                )
                {_daily_engine}
                """,
                f"""
                SELECT
                  d.ts_code AS ts_code,
                  toDate(d.trade_date) AS trade_date,
                  toFloat64OrZero(toString(d.open)) AS open,
                  toFloat64OrZero(toString(d.high)) AS high,
                  toFloat64OrZero(toString(d.low)) AS low,
                  toFloat64OrZero(toString(d.close)) AS close,
                  toFloat64OrZero(toString(d.vol)) AS vol,
                  toFloat64OrZero(toString(d.amount)) AS amount,
                  toFloat64OrZero(toString(a.adj_factor)) AS adj_factor
                FROM {TABLE_DAILY} AS d
                LEFT JOIN {TABLE_ADJ_FACTOR} AS a
                  ON d.ts_code = a.ts_code AND toDate(d.trade_date) = toDate(a.trade_date)
                """,
            ),
            (
                FAST_ADJ,
                TABLE_ADJ_FACTOR,
                f"""
                (
                  ts_code LowCardinality(String),
                  trade_date Date,
                  adj_factor Float64,
                  INDEX idx_td trade_date TYPE minmax GRANULARITY 1
                )
                {_daily_engine}
                """,
                f"""
                SELECT ts_code, toDate(trade_date) AS trade_date,
                       toFloat64OrZero(toString(adj_factor)) AS adj_factor
                FROM {TABLE_ADJ_FACTOR}
                """,
            ),
            (
                FAST_TRADE_CAL,
                TABLE_TRADE_CAL,
                """
                (
                  exchange LowCardinality(String),
                  cal_date Date,
                  is_open UInt8
                )
                ENGINE = MergeTree
                ORDER BY (exchange, is_open, cal_date)
                SETTINGS index_granularity = 256
                """,
                f"""
                SELECT exchange, toDate(cal_date) AS cal_date, toUInt8(is_open) AS is_open
                FROM {TABLE_TRADE_CAL}
                """,
            ),
            (
                FAST_TRADE_CAL_OPEN,
                TABLE_TRADE_CAL,
                """
                (
                  exchange LowCardinality(String),
                  cal_date Date
                )
                ENGINE = MergeTree
                ORDER BY (exchange, cal_date)
                SETTINGS index_granularity = 128
                """,
                f"""
                SELECT exchange, toDate(cal_date) AS cal_date
                FROM {TABLE_TRADE_CAL}
                WHERE is_open = 1
                """,
            ),
            (
                FAST_STOCK_BASIC,
                TABLE_STOCK_BASIC,
                """
                (
                  ts_code String,
                  name String,
                  list_date String,
                  delist_date String,
                  industry LowCardinality(String),
                  market LowCardinality(String),
                  list_status LowCardinality(String)
                )
                ENGINE = MergeTree
                ORDER BY ts_code
                SETTINGS index_granularity = 256
                """,
                f"""
                SELECT
                  ts_code,
                  ifNull(name, '') AS name,
                  ifNull(toString(list_date), '') AS list_date,
                  ifNull(toString(delist_date), '') AS delist_date,
                  ifNull(industry, '') AS industry,
                  ifNull(market, '') AS market,
                  ifNull(list_status, 'L') AS list_status
                FROM {TABLE_STOCK_BASIC}
                """,
            ),
            (
                FAST_DAILY_BASIC,
                TABLE_DAILY_BASIC,
                """
                (
                  trade_date Date,
                  ts_code String,
                  total_mv Float64,
                  circ_mv Float64,
                  pe Float64,
                  pb Float64
                )
                ENGINE = MergeTree
                ORDER BY (trade_date, ts_code)
                SETTINGS index_granularity = 1024
                """,
                f"""
                SELECT
                  toDate(trade_date) AS trade_date,
                  ts_code,
                  toFloat64OrZero(toString(total_mv)) AS total_mv,
                  toFloat64OrZero(toString(circ_mv)) AS circ_mv,
                  toFloat64OrZero(toString(pe)) AS pe,
                  toFloat64OrZero(toString(pb)) AS pb
                FROM {TABLE_DAILY_BASIC}
                """,
            ),
            (
                FAST_FUND_BASIC,
                TABLE_FUND_BASIC,
                """
                (
                  ts_code String,
                  name String,
                  fund_type LowCardinality(String),
                  found_date String,
                  list_date String,
                  delist_date String,
                  management String,
                  custodian String
                )
                ENGINE = MergeTree
                ORDER BY ts_code
                SETTINGS index_granularity = 256
                """,
                f"""
                SELECT
                  ts_code,
                  ifNull(name, '') AS name,
                  ifNull(fund_type, '') AS fund_type,
                  ifNull(toString(found_date), '') AS found_date,
                  ifNull(toString(list_date), '') AS list_date,
                  ifNull(toString(delist_date), '') AS delist_date,
                  ifNull(management, '') AS management,
                  ifNull(custodian, '') AS custodian
                FROM {TABLE_FUND_BASIC}
                """,
            ),
            (
                FAST_INDEX_WEIGHT,
                TABLE_INDEX_WEIGHT,
                """
                (
                  index_code LowCardinality(String),
                  trade_date Date,
                  con_code String,
                  weight Float64
                )
                ENGINE = MergeTree
                ORDER BY (index_code, trade_date, con_code)
                SETTINGS index_granularity = 512
                """,
                f"""
                SELECT
                  index_code, toDate(trade_date) AS trade_date, con_code,
                  toFloat64OrZero(toString(weight)) AS weight
                FROM {TABLE_INDEX_WEIGHT}
                """,
            ),
            (
                FAST_INDEX_DAILY,
                TABLE_INDEX_DAILY_CORE,
                f"""
                (
                  ts_code LowCardinality(String),
                  trade_date Date,
                  open Float64, high Float64, low Float64, close Float64,
                  pre_close Float64, change Float64, pct_chg Float64,
                  vol Float64, amount Float64,
                  INDEX idx_td trade_date TYPE minmax GRANULARITY 1
                )
                {_daily_engine}
                """,
                f"""
                SELECT
                  ts_code, toDate(trade_date) AS trade_date,
                  toFloat64OrZero(toString(open)) AS open,
                  toFloat64OrZero(toString(high)) AS high,
                  toFloat64OrZero(toString(low)) AS low,
                  toFloat64OrZero(toString(close)) AS close,
                  toFloat64OrZero(toString(pre_close)) AS pre_close,
                  toFloat64OrZero(toString(change)) AS change,
                  toFloat64OrZero(toString(pct_chg)) AS pct_chg,
                  toFloat64OrZero(toString(vol)) AS vol,
                  toFloat64OrZero(toString(amount)) AS amount
                FROM {TABLE_INDEX_DAILY_CORE}
                """,
            ),
        ]

        for fast_name, source, ddl_body, insert_sql in specs:
            if fast_name == FAST_DAILY_ADJ and not self.table_exists(TABLE_ADJ_FACTOR):
                logger.debug("跳过 %s：缺少源表 %s", FAST_DAILY_ADJ, TABLE_ADJ_FACTOR)
                continue
            if not self.table_exists(source):
                if source == TABLE_INDEX_DAILY_CORE and self.table_exists(TABLE_INDEX_DAILY):
                    insert_sql = insert_sql.replace(TABLE_INDEX_DAILY_CORE, TABLE_INDEX_DAILY)
                    source = TABLE_INDEX_DAILY
                else:
                    continue
            if force and self.table_exists(fast_name):
                self.command(f"DROP TABLE IF EXISTS `{fast_name}`")
                self._known_tables.discard(fast_name)
                self._missing_tables.discard(fast_name)
            self.command(f"CREATE TABLE IF NOT EXISTS `{fast_name}` {ddl_body}")
            self._missing_tables.discard(fast_name)
            self._known_tables.add(fast_name)
            cnt_df = self.query_df(f"SELECT count() AS c FROM `{fast_name}`")
            n = int(cnt_df.iloc[0]["c"]) if cnt_df is not None and not cnt_df.empty else 0
            if n > 0 and not force:
                created[fast_name] = n
                continue
            if force:
                self.command(f"TRUNCATE TABLE IF EXISTS `{fast_name}`")
            try:
                self.command(f"INSERT INTO `{fast_name}` {insert_sql}")
            except Exception as exc:
                logger.warning("填充 %s 失败: %s", fast_name, exc)
                continue
            if optimize:
                try:
                    self.command(f"OPTIMIZE TABLE `{fast_name}` FINAL")
                except Exception:
                    pass
            cnt_df = self.query_df(f"SELECT count() AS c FROM `{fast_name}`")
            n = int(cnt_df.iloc[0]["c"]) if cnt_df is not None and not cnt_df.empty else 0
            created[fast_name] = n
            logger.info("fast table ready %s rows=%s (from %s)", fast_name, n, source)

        try:
            self._write_fast_schema_version()
        except Exception as exc:
            logger.debug("写 fast schema version 失败: %s", exc)

        self._fast_ready = True
        return created

    @staticmethod
    def _codes_in_sql(codes: Sequence[str]) -> str:
        return ", ".join(f"'{_escape_sql_str(str(c))}'" for c in codes if c)

    def fetch_daily_batch(
        self,
        ts_codes: Sequence[str],
        start_date: Optional[str],
        end_date: Optional[str],
        table: Optional[str] = None,
        *,
        chunk_size: int = 300,
        columns: Optional[Sequence[str]] = None,
    ) -> pd.DataFrame:
        """多标的日线一次/分块查询（ts_code IN (...))。"""
        codes = [str(c) for c in ts_codes if c]
        if not codes:
            return pd.DataFrame()
        if table is None:
            table = self._resolve_table(FAST_DAILY, (TABLE_DAILY,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        date_pre = self._date_prewhere_sql(start, end)
        # 热列裁剪：默认 OHLCV；宽表可含 adj_factor
        if columns:
            sel_cols = [c for c in columns if c]
        else:
            sel_cols = list(_HOT_OHLCV_COLS)
        select_list: List[str] = []
        for c in ("ts_code", "trade_date", *sel_cols):
            if c not in select_list:
                select_list.append(c)
        select_sql = ", ".join(f"`{c}`" if c == "change" else c for c in select_list)

        frames: List[pd.DataFrame] = []
        for i in range(0, len(codes), max(1, int(chunk_size))):
            batch = codes[i : i + chunk_size]
            pre_parts = [f"ts_code IN ({self._codes_in_sql(batch)})"] + date_pre
            sql = f"""
                SELECT {select_sql}
                FROM {self._from_clause(table)}
                PREWHERE {' AND '.join(pre_parts)}
                ORDER BY ts_code, trade_date
                SETTINGS optimize_read_in_order = 1
            """
            part = self.query_columns(sql)
            if part is not None and not part.empty:
                frames.append(self._normalize_trade_date_col(part))
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def fetch_daily_and_adj_batch(
        self,
        ts_codes: Sequence[str],
        start_date: Optional[str],
        end_date: Optional[str],
        *,
        chunk_size: int = 300,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """多标的日线+复权：优先宽表 bt_daily_adj_fast（无 JOIN）；否则 LEFT JOIN 回退。

        返回 (daily_with_optional_adj_factor, adj_df)。
        宽表/合并路径下 adj_factor 已在 daily 上，adj_df 为空（避免上层再 merge）。
        """
        codes = [str(c) for c in ts_codes if c]
        if not codes:
            return pd.DataFrame(), pd.DataFrame()

        wide = self._resolve_table(FAST_DAILY_ADJ, ())
        if wide:
            daily = self.fetch_daily_batch(
                codes,
                start_date,
                end_date,
                table=wide,
                chunk_size=chunk_size,
                columns=_HOT_OHLCV_ADJ_COLS,
            )
            return daily, pd.DataFrame()

        daily_t = self._resolve_table(FAST_DAILY, (TABLE_DAILY,))
        adj_t = self._resolve_table(FAST_ADJ, (TABLE_ADJ_FACTOR,))
        if not daily_t or not adj_t:
            daily = self.fetch_daily_batch(codes, start_date, end_date, table=daily_t)
            return daily, pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        date_pre = self._date_prewhere_sql(start, end, col="d.trade_date")
        adj_dates = self._date_prewhere_sql(start, end)
        frames: List[pd.DataFrame] = []
        for i in range(0, len(codes), max(1, int(chunk_size))):
            batch = codes[i : i + chunk_size]
            in_sql = self._codes_in_sql(batch)
            pre_parts = [f"d.ts_code IN ({in_sql})"] + date_pre
            adj_pre = [f"ts_code IN ({in_sql})"] + adj_dates
            sql = f"""
                SELECT
                  d.ts_code AS ts_code,
                  d.trade_date AS trade_date,
                  d.open, d.high, d.low, d.close, d.vol, d.amount,
                  a.adj_factor AS adj_factor
                FROM {self._from_clause(daily_t)} AS d
                LEFT JOIN (
                  SELECT ts_code, trade_date, adj_factor
                  FROM {self._from_clause(adj_t)}
                  PREWHERE {' AND '.join(adj_pre)}
                ) AS a
                  ON d.ts_code = a.ts_code AND d.trade_date = a.trade_date
                PREWHERE {' AND '.join(pre_parts)}
                ORDER BY d.ts_code, d.trade_date
                SETTINGS join_algorithm = 'hash', optimize_read_in_order = 1
            """
            part = self.query_columns(sql)
            if part is not None and not part.empty:
                frames.append(self._normalize_trade_date_col(part))
        if not frames:
            return pd.DataFrame(), pd.DataFrame()
        return pd.concat(frames, ignore_index=True), pd.DataFrame()

    def fetch_index_daily_batch(
        self,
        ts_codes: Sequence[str],
        start_date: Optional[str],
        end_date: Optional[str],
        *,
        chunk_size: int = 300,
    ) -> pd.DataFrame:
        table = self._resolve_table(
            FAST_INDEX_DAILY, (TABLE_INDEX_DAILY_CORE, TABLE_INDEX_DAILY)
        )
        if not table:
            return pd.DataFrame()
        return self.fetch_daily_batch(
            ts_codes, start_date, end_date, table=table, chunk_size=chunk_size
        )

    # -------------------- 查询 --------------------
    def fetch_daily(
        self,
        ts_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
        table: Optional[str] = None,
    ) -> pd.DataFrame:
        if table is None:
            table = self._resolve_table(FAST_DAILY, (TABLE_DAILY,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        pre = [f"ts_code = '{_escape_sql_str(ts_code)}'"] + self._date_prewhere_sql(start, end)
        sql = f"""
            SELECT
              ts_code, trade_date,
              open, high, low, close, vol, amount
            FROM {self._from_clause(table)}
            PREWHERE {' AND '.join(pre)}
            ORDER BY trade_date
            SETTINGS optimize_read_in_order = 1
        """
        return self._normalize_trade_date_col(self.query_columns(sql))

    def fetch_daily_and_adj(
        self,
        ts_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """日线 + 复权：优先宽表；否则一次 JOIN。adj_factor 落在 daily 上时 adj 返回空。"""
        wide = self._resolve_table(FAST_DAILY_ADJ, ())
        if wide:
            start = _ymd_to_dash(start_date)
            end = _ymd_to_dash(end_date)
            pre = [f"ts_code = '{_escape_sql_str(ts_code)}'"] + self._date_prewhere_sql(start, end)
            sql = f"""
                SELECT ts_code, trade_date, open, high, low, close, vol, amount, adj_factor
                FROM {self._from_clause(wide)}
                PREWHERE {' AND '.join(pre)}
                ORDER BY trade_date
                SETTINGS optimize_read_in_order = 1
            """
            df = self._normalize_trade_date_col(self.query_columns(sql))
            return df, pd.DataFrame()

        daily_t = self._resolve_table(FAST_DAILY, (TABLE_DAILY,))
        adj_t = self._resolve_table(FAST_ADJ, (TABLE_ADJ_FACTOR,))
        if not daily_t or not adj_t:
            return self.fetch_daily(ts_code, start_date, end_date), self.fetch_adj_factor(
                ts_code, start_date, end_date
            )
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        code = _escape_sql_str(ts_code)
        date_pre = self._date_prewhere_sql(start, end, col="d.trade_date")
        adj_pre = [f"ts_code = '{code}'"] + self._date_prewhere_sql(start, end)
        pre = [f"d.ts_code = '{code}'"] + date_pre
        sql = f"""
            SELECT
              d.ts_code AS ts_code,
              d.trade_date AS trade_date,
              d.open, d.high, d.low, d.close, d.vol, d.amount,
              a.adj_factor AS adj_factor
            FROM {self._from_clause(daily_t)} AS d
            LEFT JOIN (
              SELECT ts_code, trade_date, adj_factor
              FROM {self._from_clause(adj_t)}
              PREWHERE {' AND '.join(adj_pre)}
            ) AS a
              ON d.ts_code = a.ts_code AND d.trade_date = a.trade_date
            PREWHERE {' AND '.join(pre)}
            ORDER BY d.trade_date
            SETTINGS join_algorithm = 'hash', optimize_read_in_order = 1
        """
        df = self._normalize_trade_date_col(self.query_columns(sql))
        if df is None or df.empty:
            return pd.DataFrame(), pd.DataFrame()
        return df, pd.DataFrame()

    def fetch_adj_factor(
        self,
        ts_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        table = self._resolve_table(FAST_ADJ, (TABLE_ADJ_FACTOR,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        pre = [f"ts_code = '{_escape_sql_str(ts_code)}'"] + self._date_prewhere_sql(start, end)
        sql = f"""
            SELECT
              ts_code, trade_date, adj_factor
            FROM {self._from_clause(table)}
            PREWHERE {' AND '.join(pre)}
            ORDER BY trade_date
            SETTINGS optimize_read_in_order = 1
        """
        return self._normalize_trade_date_col(self.query_columns(sql))

    def fetch_index_daily(
        self,
        ts_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        table = self._resolve_table(
            FAST_INDEX_DAILY, (TABLE_INDEX_DAILY_CORE, TABLE_INDEX_DAILY)
        )
        if not table:
            return pd.DataFrame()
        return self.fetch_daily(ts_code, start_date, end_date, table=table)

    def fetch_fund_daily(
        self,
        ts_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        if not self.table_exists(TABLE_FUND_DAILY):
            return pd.DataFrame()
        return self.fetch_daily(ts_code, start_date, end_date, table=TABLE_FUND_DAILY)

    def fetch_ohlcv(
        self,
        ts_code: str,
        asset: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        if asset == "E":
            return self.fetch_daily(ts_code, start_date, end_date)
        if asset == "I":
            return self.fetch_index_daily(ts_code, start_date, end_date)
        if asset == "FD":
            return self.fetch_fund_daily(ts_code, start_date, end_date)
        return pd.DataFrame()

    def fetch_trade_cal(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
        exchange: str = "SSE",
    ) -> pd.DataFrame:
        # 优先开市日窄表；否则全日历 + is_open 过滤由上层做
        open_t = self._resolve_table(FAST_TRADE_CAL_OPEN, ())
        table = open_t or self._resolve_table(FAST_TRADE_CAL, (TABLE_TRADE_CAL,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        prewhere = [f"exchange = '{_escape_sql_str(exchange)}'"]
        where = []
        if start:
            where.append(f"cal_date >= toDate('{start}')")
        if end:
            where.append(f"cal_date <= toDate('{end}')")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        if table == FAST_TRADE_CAL_OPEN or table.endswith("_open_fast"):
            sql = f"""
                SELECT
                  {self._date_ymd_expr("cal_date")},
                  toUInt8(1) AS is_open,
                  exchange
                FROM {self._from_clause(table)}
                PREWHERE {' AND '.join(prewhere)}
                {where_sql}
                ORDER BY cal_date
            """
        else:
            sql = f"""
                SELECT
                  {self._date_ymd_expr("cal_date")},
                  is_open,
                  exchange
                FROM {self._from_clause(table)}
                PREWHERE {' AND '.join(prewhere)}
                {where_sql}
                ORDER BY cal_date
            """
        return self._rename_ymd(self.query_df(sql), col="cal_date")

    def fetch_open_cal_ymds(
        self,
        start_date: Optional[str],
        end_date: Optional[str],
        exchange: str = "SSE",
    ) -> List[str]:
        """开市日 YYYYMMDD 列表；走 query 行结果，避免 DataFrame 组装。"""
        open_t = self._resolve_table(FAST_TRADE_CAL_OPEN, ())
        table = open_t or self._resolve_table(FAST_TRADE_CAL, (TABLE_TRADE_CAL,))
        if not table:
            return []
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        where = [f"exchange = '{_escape_sql_str(exchange)}'"]
        if start:
            where.append(f"cal_date >= toDate('{start}')")
        if end:
            where.append(f"cal_date <= toDate('{end}')")
        open_filter = ""
        if table not in (FAST_TRADE_CAL_OPEN,) and not str(table).endswith("_open_fast"):
            open_filter = " AND is_open = 1"
        sql = f"""
            SELECT toYYYYMMDD(cal_date) AS ymd
            FROM {self._from_clause(table)}
            PREWHERE {' AND '.join(where)}{open_filter}
            ORDER BY cal_date
            SETTINGS max_threads = 2
        """
        if not self.is_available():
            return []
        try:
            result = self._client().query(sql)
            out: List[str] = []
            for row in result.result_rows:
                val = row[0]
                out.append(f"{int(val):08d}" if val is not None else "")
            return [x for x in out if x]
        except Exception as exc:
            logger.debug("fetch_open_cal_ymds 失败: %s", exc)
            return []

    def fetch_prev_open_day(
        self,
        asof: str,
        exchange: str = "SSE",
    ) -> Optional[str]:
        """<= asof 的最近开市日，返回 YYYYMMDD；点查，避免拉区间。"""
        open_t = self._resolve_table(FAST_TRADE_CAL_OPEN, ())
        table = open_t or self._resolve_table(FAST_TRADE_CAL, (TABLE_TRADE_CAL,))
        if not table:
            return None
        day = _ymd_to_dash(asof)
        if not day:
            return None
        open_filter = ""
        if table not in (FAST_TRADE_CAL_OPEN,) and not str(table).endswith("_open_fast"):
            open_filter = " AND is_open = 1"
        sql = f"""
            SELECT toYYYYMMDD(cal_date) AS cal_date_ymd
            FROM {self._from_clause(table)}
            PREWHERE exchange = '{_escape_sql_str(exchange)}'
            WHERE cal_date <= toDate('{day}'){open_filter}
            ORDER BY cal_date DESC
            LIMIT 1
            SETTINGS max_threads = 1
        """
        row = self.query_one(sql)
        if not row:
            return None
        val = row.get("cal_date_ymd")
        if val is None:
            return None
        try:
            return f"{int(val):08d}"
        except Exception:
            return str(val)

    def fetch_stock_basic_row(self, ts_code: str) -> Optional[Dict[str, Any]]:
        """单票点查，避免全表加载。"""
        table = self._resolve_table(FAST_STOCK_BASIC, (TABLE_STOCK_BASIC,))
        if not table:
            return None
        cols = "ts_code, name, list_date, delist_date, industry, market, list_status"
        sql = f"""
            SELECT {cols}
            FROM {self._from_clause(table)}
            PREWHERE ts_code = '{_escape_sql_str(ts_code)}'
            LIMIT 1
            SETTINGS max_threads = 1
        """
        row = self.query_one(sql)
        if not row:
            return None
        return {
            "ts_code": str(row.get("ts_code") or ts_code),
            "name": row.get("name"),
            "list_date": row.get("list_date"),
            "delist_date": row.get("delist_date"),
            "industry": row.get("industry"),
            "market": row.get("market"),
            "list_status": row.get("list_status"),
        }

    def fetch_stock_basic(self, list_status: Optional[str] = "L") -> pd.DataFrame:
        """list_status=None/空：不按状态过滤（供历史 date 场景取 L+D+P）。"""
        table = self._resolve_table(FAST_STOCK_BASIC, (TABLE_STOCK_BASIC,))
        if not table:
            return pd.DataFrame()
        cols = "ts_code, name, list_date, delist_date, industry, market, list_status"
        if list_status:
            sql = f"""
                SELECT {cols}
                FROM {self._from_clause(table)}
                PREWHERE list_status = '{_escape_sql_str(list_status)}'
            """
        else:
            sql = f"SELECT {cols} FROM {self._from_clause(table)}"
        return self.query_df(sql)

    def fetch_daily_basic(self, trade_date: str, *, asof: bool = True) -> pd.DataFrame:
        """
        按日截面估值。asof=True 时：精确日无数据则回退到 <= 目标日的最近有数日期
        （本地库常缺最新交易日时避免空查打远程）。
        """
        table = self._resolve_table(FAST_DAILY_BASIC, (TABLE_DAILY_BASIC,))
        if not table:
            return pd.DataFrame()
        day = _ymd_to_dash(trade_date)
        if not day:
            return pd.DataFrame()
        date_pred = f"trade_date = toDate('{day}')"
        if asof:
            date_pred = (
                f"trade_date = ("
                f"SELECT max(trade_date) FROM {self._from_clause(table)} "
                f"WHERE trade_date <= toDate('{day}')"
                f")"
            )
        # asof 子查询放 WHERE；精确等值可走 PREWHERE
        if asof:
            sql = f"""
                SELECT
                  ts_code,
                  {self._date_ymd_expr("trade_date")},
                  total_mv, circ_mv, pe, pb
                FROM {self._from_clause(table)}
                WHERE {date_pred}
            """
        else:
            sql = f"""
                SELECT
                  ts_code,
                  {self._date_ymd_expr("trade_date")},
                  total_mv, circ_mv, pe, pb
                FROM {self._from_clause(table)}
                PREWHERE {date_pred}
            """
        return self._rename_ymd(self.query_df(sql))

    def fetch_daily_basic_range(self, start_date: str, end_date: str) -> pd.DataFrame:
        """区间内全部估值截面（一次扫描），供 continuously 避免逐日空查。"""
        table = self._resolve_table(FAST_DAILY_BASIC, (TABLE_DAILY_BASIC,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        if not start or not end:
            return pd.DataFrame()
        sql = f"""
            SELECT
              ts_code,
              {self._date_ymd_expr("trade_date")},
              total_mv, circ_mv, pe, pb
            FROM {self._from_clause(table)}
            WHERE trade_date >= toDate('{start}') AND trade_date <= toDate('{end}')
            ORDER BY trade_date, ts_code
        """
        return self._rename_ymd(self.query_df(sql))

    def fetch_daily_basic_max_ymd(self) -> Optional[str]:
        table = self._resolve_table(FAST_DAILY_BASIC, (TABLE_DAILY_BASIC,))
        if not table:
            return None
        row = self.query_one(
            f"SELECT toYYYYMMDD(max(trade_date)) AS ymd FROM {self._from_clause(table)}"
        )
        if not row or row.get("ymd") is None:
            return None
        try:
            return f"{int(row['ymd']):08d}"
        except Exception:
            return str(row["ymd"])

    def fetch_fund_basic(self, ts_code: str) -> pd.DataFrame:
        table = self._resolve_table(FAST_FUND_BASIC, (TABLE_FUND_BASIC,))
        if not table:
            return pd.DataFrame()
        sql = f"""
            SELECT ts_code, name, fund_type, found_date, list_date, delist_date, management, custodian
            FROM {self._from_clause(table)}
            PREWHERE ts_code = '{_escape_sql_str(ts_code)}'
            LIMIT 1
            SETTINGS max_threads = 1
        """
        row = self.query_one(sql)
        if not row:
            return pd.DataFrame()
        return pd.DataFrame([row])

    def fetch_index_weight(
        self,
        index_code: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        table = self._resolve_table(FAST_INDEX_WEIGHT, (TABLE_INDEX_WEIGHT,))
        if not table:
            return pd.DataFrame()
        start = _ymd_to_dash(start_date)
        end = _ymd_to_dash(end_date)
        prewhere = [f"index_code = '{_escape_sql_str(index_code)}'"]
        where = []
        if start:
            where.append(f"trade_date >= toDate('{start}')")
        if end:
            where.append(f"trade_date <= toDate('{end}')")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT
              index_code, con_code,
              {self._date_ymd_expr("trade_date")},
              weight
            FROM {self._from_clause(table)}
            PREWHERE {' AND '.join(prewhere)}
            {where_sql}
            ORDER BY trade_date
        """
        return self._rename_ymd(self.query_df(sql))

    def fetch_index_weight_asof(self, index_code: str, asof_date: str) -> pd.DataFrame:
        """
        取 <= asof 的最近一期权重。

        小表上「相关子查询 max(trade_date)」会多一次扫描/计划开销，反而慢于
        单次有序扫描后再在结果里截取最新 trade_date（实测 ~8ms vs ~12ms）。
        """
        table = self._resolve_table(FAST_INDEX_WEIGHT, (TABLE_INDEX_WEIGHT,))
        if not table:
            return pd.DataFrame()
        day = _ymd_to_dash(asof_date)
        if not day:
            return pd.DataFrame()
        code = _escape_sql_str(index_code)
        sql = f"""
            SELECT
              index_code, con_code,
              {self._date_ymd_expr("trade_date")},
              weight
            FROM {self._from_clause(table)}
            PREWHERE index_code = '{code}'
            WHERE trade_date <= toDate('{day}')
            ORDER BY trade_date DESC
        """
        df = self._rename_ymd(self.query_df(sql))
        if df is None or df.empty or "trade_date" not in df.columns:
            return df if df is not None else pd.DataFrame()
        latest = df["trade_date"].iloc[0]
        return df[df["trade_date"] == latest].copy()


def build_clickhouse_client(provider_config: Optional[Dict[str, Any]]) -> Optional[TushareClickHouseClient]:
    """从 Provider config / 环境变量构造客户端；未启用则返回 None。"""
    cfg = dict(provider_config or {})
    ch_cfg = cfg.get("clickhouse")
    if isinstance(ch_cfg, dict):
        merged = dict(ch_cfg)
    else:
        merged = {}
    for key in (
        "enabled",
        "host",
        "port",
        "user",
        "password",
        "db_name",
        "config_path",
        "use_final",
        "prefer_fast",
        "auto_ensure_fast",
    ):
        if key not in merged and cfg.get(key) is not None:
            merged[key] = cfg.get(key)
        flat = cfg.get(f"clickhouse_{key}")
        if key not in merged and flat is not None:
            merged[key] = flat
        if key not in merged and key == "enabled" and cfg.get("clickhouse") in (True, False, 0, 1, "0", "1"):
            merged["enabled"] = cfg.get("clickhouse")

    client = TushareClickHouseClient(merged)
    if not client.enabled:
        return None
    return client
