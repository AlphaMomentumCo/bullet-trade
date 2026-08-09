"""
作者: BruceLee
文件职责: 管理单次回测运行内的临时数据会话、内存预算、QMT 下载记录和统计。
主要输入: 回测起止时间、数据源名称、环境变量或 CLI 传入的会话配置、provider 读取结果。
主要输出: 会话级命中/下载/降级统计、可选 manifest、供 provider 查询的当前回测会话。
上下游关系: 由 BacktestEngine.run 激活和清理，被 data.api 与 MiniQMTProvider 在回测路径中读取。
关键约定: 默认关闭；仅回测启用；不写永久行情缓存；不得影响实盘 live provider 或 QMT server。
"""

from __future__ import annotations

import contextvars
import json
import os
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from ..utils.env_loader import parse_bool

_CURRENT_SESSION: contextvars.ContextVar[Optional["BacktestDataSession"]] = contextvars.ContextVar(
    "bullet_trade_backtest_data_session", default=None
)

_DEFAULT_MAX_CACHE_BYTES = 512 * 1024 * 1024


def _coerce_int(value: Any, default: int) -> int:
    """
    将配置值转换为整数。

    Args:
        value: 待转换的配置值。
        default: 转换失败或未提供时返回的默认值。

    Returns:
        int: 转换后的整数。
    """
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_datetime(value: Any) -> Optional[datetime]:
    """
    将日期或时间配置转换为 datetime。

    Args:
        value: 字符串、date、datetime、pandas 时间戳或空值。

    Returns:
        Optional[datetime]: 成功转换后的 datetime，空值或失败时返回 None。
    """
    if value in (None, "", "NaT"):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, Date):
        return datetime.combine(value, Time(0, 0))
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _format_manifest_time(value: Optional[datetime]) -> Optional[str]:
    """
    将时间格式化为 manifest 友好的字符串。

    Args:
        value: 待格式化的时间。

    Returns:
        Optional[str]: ISO 风格时间字符串，空值返回 None。
    """
    if value is None:
        return None
    try:
        return value.isoformat(sep=" ")
    except Exception:
        return str(value)


def _estimate_value_bytes(value: Any) -> int:
    """
    估算缓存对象占用的内存字节数。

    Args:
        value: DataFrame、Series 或其他 Python 对象。

    Returns:
        int: 估算字节数，无法精确估算时使用 sys.getsizeof。
    """
    if isinstance(value, pd.DataFrame):
        try:
            return int(value.memory_usage(deep=True).sum())
        except Exception:
            return int(sys.getsizeof(value))
    if isinstance(value, pd.Series):
        try:
            return int(value.memory_usage(deep=True))
        except Exception:
            return int(sys.getsizeof(value))
    return int(sys.getsizeof(value))


def _available_memory_bytes() -> Optional[int]:
    """
    读取系统当前可用内存。

    Returns:
        Optional[int]: 可用内存字节数；缺少可用观测能力时返回 None。
    """
    try:
        import psutil  # type: ignore

        return int(psutil.virtual_memory().available)
    except Exception:
        pass

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return int(parts[1]) * 1024
        except Exception:
            return None
    return None


def _normalize_period_for_window(period: str) -> str:
    """
    规范化周期文本，用于判断日线或分钟线窗口。

    Args:
        period: provider 或 QMT 使用的周期文本。

    Returns:
        str: 规范化后的周期文本。
    """
    value = str(period or "").lower()
    if value in ("daily", "day", "1day", "d"):
        return "1d"
    if value in ("minute", "min", "1minute", "m"):
        return "1m"
    return value or "1d"


def _window_start_from_count(
    end_dt: Optional[datetime], period: str, count: Optional[int]
) -> Optional[datetime]:
    """
    根据 count 请求估算需要覆盖的起始时间。

    Args:
        end_dt: 请求结束时间。
        period: 行情周期。
        count: 请求条数。

    Returns:
        Optional[datetime]: 估算起始时间；无法估算时返回 None。
    """
    if end_dt is None or not count:
        return None
    normalized = _normalize_period_for_window(period)
    if "m" in normalized:
        days = max(int(count / 240) + 3, 3)
    else:
        days = max(int(count) * 2, 30)
    return end_dt - timedelta(days=days)


def _normalize_coverage_dt(
    value: Optional[datetime], period: str, *, is_end: bool
) -> Optional[datetime]:
    """
    将覆盖校验时间按周期对齐。

    Args:
        value: 待对齐的时间。
        period: 行情周期。
        is_end: 是否为结束时间。

    Returns:
        Optional[datetime]: 对齐后的时间。
    """
    if value is None:
        return None
    normalized = _normalize_period_for_window(period)
    if "d" in normalized:
        return datetime.combine(value.date(), Time(0, 0))
    if is_end and value.time() == Time(0, 0):
        return datetime.combine(value.date(), Time(15, 0))
    return value


@dataclass
class BacktestDataSessionConfig:
    """
    回测数据会话配置。

    Attributes:
        enabled: 是否启用数据会话优化。
        qmt_download_dedup: 是否启用 MiniQMT 回测下载去重。
        price_block_cache_enabled: 是否启用行情块缓存。
        current_bar_cache_enabled: 是否启用 current data bar 快照。
        max_cache_bytes: 本次会话允许使用的最大缓存内存。
        min_free_memory_bytes: 系统最小剩余内存保护阈值。
        manifest_path: 可选 manifest 输出路径。
        provider_name: 当前回测数据源名称。
        start_date: 回测开始时间。
        end_date: 回测结束时间。
        frequency: 回测频率。
        qmt_require_coverage: QMT 下载后覆盖不足时是否抛错。
    """

    enabled: bool = False
    qmt_download_dedup: bool = True
    price_block_cache_enabled: bool = False
    current_bar_cache_enabled: bool = True
    max_cache_bytes: int = _DEFAULT_MAX_CACHE_BYTES
    min_free_memory_bytes: int = 0
    manifest_path: Optional[str] = None
    provider_name: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    frequency: str = "day"
    qmt_require_coverage: bool = False

    @classmethod
    def from_env(
        cls,
        *,
        overrides: Optional[Dict[str, Any]] = None,
        start_date: Any = None,
        end_date: Any = None,
        frequency: str = "day",
        provider_name: Optional[str] = None,
    ) -> "BacktestDataSessionConfig":
        """
        从环境变量和显式覆盖项构造配置。

        Args:
            overrides: CLI 或调用方传入的覆盖配置。
            start_date: 回测开始日期。
            end_date: 回测结束日期。
            frequency: 回测频率。
            provider_name: 当前 provider 名称。

        Returns:
            BacktestDataSessionConfig: 合并后的会话配置。
        """
        overrides = dict(overrides or {})
        enabled_value = overrides.pop("enabled", os.getenv("BT_BACKTEST_DATA_SESSION"))
        enabled = parse_bool(enabled_value, default=False)
        # 会话开启且未显式配置 PRICE_BLOCKS 时，默认打开行情块缓存
        if "price_block_cache_enabled" in overrides:
            price_block_raw = overrides.pop("price_block_cache_enabled")
            price_block_default = False
        else:
            price_block_env = os.getenv("BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS")
            if price_block_env is None:
                price_block_raw = enabled
                price_block_default = bool(enabled)
            else:
                price_block_raw = price_block_env
                price_block_default = False
        config = cls(
            enabled=enabled,
            qmt_download_dedup=parse_bool(
                overrides.pop(
                    "qmt_download_dedup",
                    os.getenv("BT_BACKTEST_DATA_SESSION_QMT_DEDUP", "true"),
                ),
                default=True,
            ),
            price_block_cache_enabled=parse_bool(
                price_block_raw,
                default=price_block_default,
            ),
            current_bar_cache_enabled=parse_bool(
                overrides.pop(
                    "current_bar_cache_enabled",
                    os.getenv("BT_BACKTEST_DATA_SESSION_CURRENT_BAR", "true"),
                ),
                default=True,
            ),
            max_cache_bytes=_coerce_int(
                overrides.pop(
                    "max_cache_bytes",
                    os.getenv("BT_BACKTEST_DATA_SESSION_MAX_BYTES"),
                ),
                _DEFAULT_MAX_CACHE_BYTES,
            ),
            min_free_memory_bytes=_coerce_int(
                overrides.pop(
                    "min_free_memory_bytes",
                    os.getenv("BT_BACKTEST_DATA_SESSION_MIN_FREE_BYTES"),
                ),
                0,
            ),
            manifest_path=overrides.pop(
                "manifest_path",
                os.getenv("BT_BACKTEST_DATA_SESSION_MANIFEST") or None,
            ),
            provider_name=provider_name,
            start_date=_coerce_datetime(start_date),
            end_date=_coerce_datetime(end_date),
            frequency=frequency or "day",
            qmt_require_coverage=parse_bool(
                overrides.pop(
                    "qmt_require_coverage",
                    os.getenv("BT_BACKTEST_DATA_SESSION_QMT_REQUIRE_COVERAGE", "false"),
                ),
                default=False,
            ),
        )
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config


@dataclass
class BacktestDataSessionStats:
    """
    回测数据会话统计。

    Attributes:
        cache_hits: 行情块缓存命中次数。
        cache_misses: 行情块缓存未命中次数。
        cache_writes: 行情块写入次数。
        current_bar_hits: current bar 快照命中次数。
        current_bar_misses: current bar 快照未命中次数。
        current_bar_writes: current bar 快照写入次数。
        downloads: QMT 实际下载次数。
        skipped_downloads: QMT 覆盖命中后跳过下载次数。
        download_failures: QMT 下载异常次数。
        coverage_failures: QMT 下载后覆盖校验失败次数。
        evictions: 内存预算驱逐次数。
        degradations: 因预算或参数不支持而降级次数。
        errors: 会话内部错误次数。
        cache_bytes: 当前估算缓存字节数。
        peak_cache_bytes: 峰值估算缓存字节数。
    """

    cache_hits: int = 0
    cache_misses: int = 0
    cache_writes: int = 0
    current_bar_hits: int = 0
    current_bar_misses: int = 0
    current_bar_writes: int = 0
    downloads: int = 0
    skipped_downloads: int = 0
    download_failures: int = 0
    coverage_failures: int = 0
    evictions: int = 0
    degradations: int = 0
    errors: int = 0
    cache_bytes: int = 0
    peak_cache_bytes: int = 0

    def to_dict(self) -> Dict[str, int]:
        """
        转换为普通字典。

        Returns:
            Dict[str, int]: 统计字段字典。
        """
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_writes": self.cache_writes,
            "current_bar_hits": self.current_bar_hits,
            "current_bar_misses": self.current_bar_misses,
            "current_bar_writes": self.current_bar_writes,
            "downloads": self.downloads,
            "skipped_downloads": self.skipped_downloads,
            "download_failures": self.download_failures,
            "coverage_failures": self.coverage_failures,
            "evictions": self.evictions,
            "degradations": self.degradations,
            "errors": self.errors,
            "cache_bytes": self.cache_bytes,
            "peak_cache_bytes": self.peak_cache_bytes,
        }


@dataclass
class _CachedBlock:
    """
    内存行情块记录。

    Attributes:
        key: 缓存键。
        value: 缓存对象。
        bytes_used: 估算内存字节数。
        rows: 行数。
        created_at: 创建时间。
        last_used_at: 最近访问时间。
    """

    key: Tuple[Any, ...]
    value: Any
    bytes_used: int
    rows: int
    created_at: float
    last_used_at: float


@dataclass
class _DateTimeRange:
    """
    已覆盖的数据时间区间。

    Attributes:
        start: 覆盖起始时间。
        end: 覆盖结束时间。
        strict_start: 是否可用于严格起始时间覆盖判断。
    """

    start: Optional[datetime]
    end: Optional[datetime]
    strict_start: bool = True

    def covers(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
        *,
        require_strict_start: bool,
    ) -> bool:
        """
        判断本区间是否覆盖目标区间。

        Args:
            start: 目标起始时间。
            end: 目标结束时间。
            require_strict_start: 目标请求是否要求严格校验起始时间。

        Returns:
            bool: 完全覆盖时返回 True。
        """
        if require_strict_start:
            if not self.strict_start:
                return False
            if start is not None and self.start is not None and self.start > start:
                return False
            if start is not None and self.start is None:
                return False
        if end is not None and self.end is not None and self.end < end:
            return False
        if end is not None and self.end is None:
            return False
        return True


class BacktestDataSession:
    """
    单次回测运行内的数据会话。

    该对象只保存进程内临时状态，负责 QMT 下载去重、内存预算、current bar 快照、
    行情块统计和 manifest 输出。它不持久化行情数据，也不改变实盘路径。
    """

    def __init__(self, config: BacktestDataSessionConfig) -> None:
        """
        初始化回测数据会话。

        Args:
            config: 回测数据会话配置。
        """
        self.config = config
        self.stats = BacktestDataSessionStats()
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now()
        self.finished_at: Optional[datetime] = None
        self.closed = False
        self._price_blocks: "OrderedDict[Tuple[Any, ...], _CachedBlock]" = OrderedDict()
        self._current_bar_id: Optional[datetime] = None
        self._current_bar_values: Dict[Tuple[Any, ...], Any] = {}
        self._qmt_ranges: Dict[Tuple[str, str, str], List[_DateTimeRange]] = {}
        self.qmt_manifest: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []

    @property
    def active(self) -> bool:
        """
        判断会话当前是否可用。

        Returns:
            bool: 配置启用且尚未关闭时返回 True。
        """
        return bool(self.config.enabled and not self.closed)

    def close(self) -> None:
        """
        关闭会话并输出 manifest。

        Side Effects:
            清空内存缓存，写入可选 manifest，并将会话标记为 closed。
        """
        if self.closed:
            return
        self.finished_at = datetime.now()
        try:
            self.write_manifest()
        finally:
            self._price_blocks.clear()
            self._current_bar_values.clear()
            self.closed = True

    def to_manifest(self) -> Dict[str, Any]:
        """
        生成 manifest 字典。

        Returns:
            Dict[str, Any]: 包含配置、统计、QMT 下载记录和事件的 manifest。
        """
        elapsed = time.monotonic() - self.started_monotonic
        return {
            "enabled": self.config.enabled,
            "provider_name": self.config.provider_name,
            "frequency": self.config.frequency,
            "start_date": _format_manifest_time(self.config.start_date),
            "end_date": _format_manifest_time(self.config.end_date),
            "started_at": _format_manifest_time(self.started_at),
            "finished_at": _format_manifest_time(self.finished_at),
            "elapsed_seconds": elapsed,
            "config": {
                "qmt_download_dedup": self.config.qmt_download_dedup,
                "price_block_cache_enabled": self.config.price_block_cache_enabled,
                "current_bar_cache_enabled": self.config.current_bar_cache_enabled,
                "max_cache_bytes": self.config.max_cache_bytes,
                "min_free_memory_bytes": self.config.min_free_memory_bytes,
                "qmt_require_coverage": self.config.qmt_require_coverage,
            },
            "stats": self.stats.to_dict(),
            "qmt_downloads": self.qmt_manifest,
            "events": self.events,
        }

    def write_manifest(self) -> None:
        """
        将 manifest 写入磁盘。

        Side Effects:
            当 manifest_path 已配置时创建父目录并写入 JSON 文件。
        """
        path = self.config.manifest_path
        if not path:
            return
        manifest_path = Path(path).expanduser()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_manifest()
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def record_event(self, event_type: str, **payload: Any) -> None:
        """
        记录会话事件。

        Args:
            event_type: 事件类型。
            **payload: 事件附加字段。
        """
        event = {
            "type": event_type,
            "time": _format_manifest_time(datetime.now()),
        }
        event.update(payload)
        self.events.append(event)

    def record_degradation(self, reason: str, **payload: Any) -> None:
        """
        记录降级事件。

        Args:
            reason: 降级原因。
            **payload: 降级附加字段。
        """
        self.stats.degradations += 1
        self.record_event("degradation", reason=reason, **payload)

    def advance_bar(self, current_dt: Optional[datetime]) -> None:
        """
        推进当前 bar 并在切换时清理 current data 快照。

        Args:
            current_dt: 新的回测时间。

        Side Effects:
            当前 bar 变化时清空 current bar 快照。
        """
        if not self.active or current_dt is None:
            return
        if self._current_bar_id != current_dt:
            self._current_bar_id = current_dt
            self._current_bar_values.clear()

    def get_current_bar_value(self, key: Tuple[Any, ...]) -> Optional[Any]:
        """
        读取 current bar 快照。

        Args:
            key: 快照键。

        Returns:
            Optional[Any]: 命中时返回快照值，未命中或关闭时返回 None。
        """
        if not self.active or not self.config.current_bar_cache_enabled:
            return None
        if key in self._current_bar_values:
            self.stats.current_bar_hits += 1
            return self._current_bar_values[key]
        self.stats.current_bar_misses += 1
        return None

    def set_current_bar_value(self, key: Tuple[Any, ...], value: Any) -> None:
        """
        写入 current bar 快照。

        Args:
            key: 快照键。
            value: 快照值。
        """
        if not self.active or not self.config.current_bar_cache_enabled:
            return
        self._current_bar_values[key] = value
        self.stats.current_bar_writes += 1

    def get_price_block(self, key: Tuple[Any, ...]) -> Optional[Any]:
        """
        读取行情块缓存。

        Args:
            key: 行情块缓存键。

        Returns:
            Optional[Any]: 命中时返回缓存对象，未命中或关闭时返回 None。
        """
        if not self.active or not self.config.price_block_cache_enabled:
            return None
        block = self._price_blocks.get(key)
        if block is None:
            self.stats.cache_misses += 1
            return None
        block.last_used_at = time.monotonic()
        self._price_blocks.move_to_end(key)
        self.stats.cache_hits += 1
        return block.value

    def set_price_block(
        self, key: Tuple[Any, ...], value: Any, *, rows: Optional[int] = None
    ) -> bool:
        """
        写入行情块缓存并执行预算控制。

        Args:
            key: 行情块缓存键。
            value: 要缓存的数据。
            rows: 可选行数。

        Returns:
            bool: 成功缓存返回 True；因预算或关闭而跳过返回 False。
        """
        if not self.active or not self.config.price_block_cache_enabled:
            return False
        bytes_used = _estimate_value_bytes(value)
        if not self._ensure_budget(bytes_used):
            self.record_degradation("cache_budget_exceeded", key=str(key), bytes_used=bytes_used)
            return False
        if key in self._price_blocks:
            old = self._price_blocks.pop(key)
            self.stats.cache_bytes = max(0, self.stats.cache_bytes - old.bytes_used)
        row_count = rows
        if row_count is None and hasattr(value, "__len__"):
            try:
                row_count = len(value)
            except Exception:
                row_count = 0
        block = _CachedBlock(
            key=key,
            value=value,
            bytes_used=bytes_used,
            rows=int(row_count or 0),
            created_at=time.monotonic(),
            last_used_at=time.monotonic(),
        )
        self._price_blocks[key] = block
        self.stats.cache_writes += 1
        self.stats.cache_bytes += bytes_used
        self.stats.peak_cache_bytes = max(self.stats.peak_cache_bytes, self.stats.cache_bytes)
        return True

    def _ensure_budget(self, incoming_bytes: int) -> bool:
        """
        确认新增缓存不会突破内存预算。

        Args:
            incoming_bytes: 准备新增的缓存字节数。

        Returns:
            bool: 预算允许返回 True，否则返回 False。
        """
        free_bytes = _available_memory_bytes()
        if (
            free_bytes is not None
            and self.config.min_free_memory_bytes > 0
            and free_bytes < self.config.min_free_memory_bytes
        ):
            self.record_degradation(
                "min_free_memory_reached",
                free_bytes=free_bytes,
                min_free_memory_bytes=self.config.min_free_memory_bytes,
            )
            return False

        if incoming_bytes > self.config.max_cache_bytes:
            return False
        while (
            self.stats.cache_bytes + incoming_bytes > self.config.max_cache_bytes
            and self._price_blocks
        ):
            self._evict_oldest("max_cache_bytes")
        return self.stats.cache_bytes + incoming_bytes <= self.config.max_cache_bytes

    def _evict_oldest(self, reason: str) -> None:
        """
        驱逐最久未使用的行情块。

        Args:
            reason: 驱逐原因。
        """
        key, block = self._price_blocks.popitem(last=False)
        self.stats.cache_bytes = max(0, self.stats.cache_bytes - block.bytes_used)
        self.stats.evictions += 1
        self.record_event(
            "eviction",
            reason=reason,
            key=str(key),
            rows=block.rows,
            bytes_used=block.bytes_used,
        )

    def resolve_qmt_window(
        self,
        *,
        period: str,
        start_date: Any,
        end_date: Any,
        count: Optional[int],
    ) -> Tuple[Optional[datetime], Optional[datetime], bool]:
        """
        根据请求和回测区间计算 QMT 下载覆盖窗口。

        Args:
            period: QMT 周期。
            start_date: 本次行情请求起始时间。
            end_date: 本次行情请求结束时间。
            count: 本次行情请求条数。

        Returns:
            Tuple[Optional[datetime], Optional[datetime], bool]: 起始时间、结束时间、是否需要严格校验起始覆盖。
        """
        request_start = _coerce_datetime(start_date)
        request_end = _coerce_datetime(end_date)
        session_start = self.config.start_date
        session_end = self.config.end_date

        strict_start = request_start is not None and count is None
        derived_start = _window_start_from_count(request_end or session_start, period, count)
        starts = [
            item for item in (request_start, derived_start, session_start) if item is not None
        ]
        ends = [item for item in (request_end, session_end) if item is not None]

        start = min(starts) if starts else None
        end = max(ends) if ends else None
        normalized_start = _normalize_coverage_dt(start, period, is_end=False)
        normalized_end = _normalize_coverage_dt(end, period, is_end=True)
        return normalized_start, normalized_end, strict_start

    def ensure_qmt_downloaded(
        self,
        *,
        provider_name: str,
        security: str,
        period: str,
        start_date: Any,
        end_date: Any,
        count: Optional[int],
        download_fn: Callable[[Optional[datetime], Optional[datetime]], None],
        local_data_fn: Callable[[Optional[datetime], Optional[datetime]], pd.DataFrame],
    ) -> bool:
        """
        确保 QMT 本地数据覆盖回测窗口，并对重复下载去重。

        Args:
            provider_name: provider 名称。
            security: QMT 格式证券代码。
            period: QMT 周期。
            start_date: 本次请求起始时间。
            end_date: 本次请求结束时间。
            count: 本次请求条数。
            download_fn: 触发 QMT 下载的回调。
            local_data_fn: 读取本地数据用于覆盖校验的回调。

        Returns:
            bool: 已确认覆盖或已执行下载流程返回 True；未启用去重返回 False。

        Raises:
            Exception: 当下载回调抛错时保持原异常语义。
        """
        if not self.active or not self.config.qmt_download_dedup:
            return False
        start, end, strict_start = self.resolve_qmt_window(
            period=period,
            start_date=start_date,
            end_date=end_date,
            count=count,
        )
        key = (provider_name or "unknown", security, period)
        if self._qmt_range_covers(key, start, end, strict_start=strict_start):
            self.stats.skipped_downloads += 1
            self._append_qmt_manifest(
                key=key,
                requested_start=start,
                requested_end=end,
                actual_start=None,
                actual_end=None,
                rows=None,
                downloaded=False,
                skipped=True,
                coverage_ok=True,
                download_seconds=0.0,
                read_seconds=0.0,
                message="covered_by_session",
            )
            return True

        download_start = time.monotonic()
        try:
            download_fn(start, end)
        except Exception:
            self.stats.download_failures += 1
            self._append_qmt_manifest(
                key=key,
                requested_start=start,
                requested_end=end,
                actual_start=None,
                actual_end=None,
                rows=None,
                downloaded=True,
                skipped=False,
                coverage_ok=False,
                download_seconds=time.monotonic() - download_start,
                read_seconds=0.0,
                message="download_failed",
            )
            raise
        download_seconds = time.monotonic() - download_start
        self.stats.downloads += 1

        read_start = time.monotonic()
        local_df = local_data_fn(start, end)
        read_seconds = time.monotonic() - read_start
        coverage_ok, actual_start, actual_end, rows, message = self._inspect_qmt_coverage(
            local_df,
            period=period,
            requested_start=start,
            requested_end=end,
            strict_start=strict_start,
        )
        if coverage_ok:
            registered_start = actual_start if strict_start else start
            self._register_qmt_range(
                key,
                registered_start,
                actual_end or end,
                strict_start=strict_start,
            )
        else:
            self.stats.coverage_failures += 1

        self._append_qmt_manifest(
            key=key,
            requested_start=start,
            requested_end=end,
            actual_start=actual_start,
            actual_end=actual_end,
            rows=rows,
            downloaded=True,
            skipped=False,
            coverage_ok=coverage_ok,
            download_seconds=download_seconds,
            read_seconds=read_seconds,
            message=message,
        )
        return True

    def _qmt_range_covers(
        self,
        key: Tuple[str, str, str],
        start: Optional[datetime],
        end: Optional[datetime],
        *,
        strict_start: bool,
    ) -> bool:
        """
        判断 QMT 已下载记录是否覆盖目标区间。

        Args:
            key: provider、证券和周期组成的键。
            start: 目标起始时间。
            end: 目标结束时间。
            strict_start: 目标请求是否要求严格起始覆盖。

        Returns:
            bool: 已覆盖返回 True。
        """
        ranges = self._qmt_ranges.get(key, [])
        return any(item.covers(start, end, require_strict_start=strict_start) for item in ranges)

    def _register_qmt_range(
        self,
        key: Tuple[str, str, str],
        start: Optional[datetime],
        end: Optional[datetime],
        *,
        strict_start: bool,
    ) -> None:
        """
        登记 QMT 已覆盖区间。

        Args:
            key: provider、证券和周期组成的键。
            start: 覆盖起始时间。
            end: 覆盖结束时间。
            strict_start: 该记录是否可用于严格起始覆盖。
        """
        ranges = self._qmt_ranges.setdefault(key, [])
        ranges.append(_DateTimeRange(start=start, end=end, strict_start=strict_start))
        ranges.sort(key=lambda item: item.start or datetime.min)
        merged: List[_DateTimeRange] = []
        for item in ranges:
            if not merged:
                merged.append(item)
                continue
            last = merged[-1]
            if last.strict_start == item.strict_start and (
                last.end is None or item.start is None or last.end >= item.start
            ):
                if last.end is None or item.end is None:
                    last.end = None
                else:
                    last.end = max(last.end, item.end)
                if last.start is None or (item.start is not None and item.start < last.start):
                    last.start = item.start
            else:
                merged.append(item)
        self._qmt_ranges[key] = merged

    def _inspect_qmt_coverage(
        self,
        df: pd.DataFrame,
        *,
        period: str,
        requested_start: Optional[datetime],
        requested_end: Optional[datetime],
        strict_start: bool,
    ) -> Tuple[bool, Optional[datetime], Optional[datetime], int, str]:
        """
        检查 QMT 本地数据是否覆盖目标区间。

        Args:
            df: 从 QMT 本地读取的行情数据。
            period: QMT 周期。
            requested_start: 目标起始时间。
            requested_end: 目标结束时间。
            strict_start: 是否严格校验起始时间。

        Returns:
            Tuple[bool, Optional[datetime], Optional[datetime], int, str]: 是否覆盖、实际起止、行数和说明。
        """
        if df is None or df.empty:
            return False, None, None, 0, "local_data_empty"
        try:
            idx = pd.DatetimeIndex(pd.to_datetime(df.index))
        except Exception:
            return False, None, None, len(df), "invalid_datetime_index"
        if idx.empty:
            return False, None, None, 0, "local_index_empty"

        actual_start = idx.min().to_pydatetime()
        actual_end = idx.max().to_pydatetime()
        expected_start = _normalize_coverage_dt(requested_start, period, is_end=False)
        expected_end = _normalize_coverage_dt(requested_end, period, is_end=True)

        if strict_start and expected_start is not None and actual_start > expected_start:
            return False, actual_start, actual_end, len(df), "start_not_covered"
        if expected_end is not None and actual_end < expected_end:
            return False, actual_start, actual_end, len(df), "end_not_covered"
        return True, actual_start, actual_end, len(df), "covered"

    def _append_qmt_manifest(
        self,
        *,
        key: Tuple[str, str, str],
        requested_start: Optional[datetime],
        requested_end: Optional[datetime],
        actual_start: Optional[datetime],
        actual_end: Optional[datetime],
        rows: Optional[int],
        downloaded: bool,
        skipped: bool,
        coverage_ok: bool,
        download_seconds: float,
        read_seconds: float,
        message: str,
    ) -> None:
        """
        追加 QMT 下载 manifest 记录。

        Args:
            key: provider、证券和周期组成的键。
            requested_start: 请求覆盖起始时间。
            requested_end: 请求覆盖结束时间。
            actual_start: 实际本地数据起始时间。
            actual_end: 实际本地数据结束时间。
            rows: 本地数据行数。
            downloaded: 是否实际调用下载。
            skipped: 是否因已覆盖跳过下载。
            coverage_ok: 覆盖校验是否通过。
            download_seconds: 下载耗时。
            read_seconds: 校验读取耗时。
            message: 状态说明。
        """
        provider_name, security, period = key
        self.qmt_manifest.append(
            {
                "provider": provider_name,
                "security": security,
                "period": period,
                "requested_start": _format_manifest_time(requested_start),
                "requested_end": _format_manifest_time(requested_end),
                "actual_start": _format_manifest_time(actual_start),
                "actual_end": _format_manifest_time(actual_end),
                "rows": rows,
                "downloaded": downloaded,
                "skipped": skipped,
                "coverage_ok": coverage_ok,
                "download_seconds": round(float(download_seconds), 6),
                "read_seconds": round(float(read_seconds), 6),
                "message": message,
            }
        )


def create_backtest_data_session(
    *,
    overrides: Optional[Dict[str, Any]] = None,
    start_date: Any = None,
    end_date: Any = None,
    frequency: str = "day",
    provider_name: Optional[str] = None,
) -> BacktestDataSession:
    """
    创建回测数据会话。

    Args:
        overrides: 显式覆盖配置。
        start_date: 回测开始日期。
        end_date: 回测结束日期。
        frequency: 回测频率。
        provider_name: 当前 provider 名称。

    Returns:
        BacktestDataSession: 新建的回测数据会话。
    """
    config = BacktestDataSessionConfig.from_env(
        overrides=overrides,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        provider_name=provider_name,
    )
    return BacktestDataSession(config)


def set_current_backtest_data_session(
    session: Optional[BacktestDataSession],
) -> contextvars.Token[Optional[BacktestDataSession]]:
    """
    设置当前执行上下文中的回测数据会话。

    Args:
        session: 要设置的会话，None 表示清空。

    Returns:
        contextvars.Token: 用于恢复旧值的 token。
    """
    return _CURRENT_SESSION.set(session)


def reset_current_backtest_data_session(
    token: contextvars.Token[Optional[BacktestDataSession]],
) -> None:
    """
    恢复当前执行上下文中的回测数据会话。

    Args:
        token: set_current_backtest_data_session 返回的 token。
    """
    _CURRENT_SESSION.reset(token)


def get_current_backtest_data_session() -> Optional[BacktestDataSession]:
    """
    获取当前启用中的回测数据会话。

    Returns:
        Optional[BacktestDataSession]: 当前会话；未启用或已关闭时返回 None。
    """
    session = _CURRENT_SESSION.get()
    if session is None or not session.active:
        return None
    return session


def active_backtest_data_session_stats() -> Optional[Dict[str, int]]:
    """
    返回当前会话统计快照。

    Returns:
        Optional[Dict[str, int]]: 当前会话统计；没有会话时返回 None。
    """
    session = get_current_backtest_data_session()
    if session is None:
        return None
    return session.stats.to_dict()


__all__ = [
    "BacktestDataSession",
    "BacktestDataSessionConfig",
    "BacktestDataSessionStats",
    "active_backtest_data_session_stats",
    "create_backtest_data_session",
    "get_current_backtest_data_session",
    "reset_current_backtest_data_session",
    "set_current_backtest_data_session",
]
