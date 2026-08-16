# BulletTrade · `delay_analyse` 分支说明

本分支聚焦**回测取数延迟分析与热路径优化**。下文详细记录相对主干的主要改动；产品级完整文档仍见仓库 `docs/`（本 README 不再重复）。

---

## 一、问题背景（改动动机）

回测墙钟时间往往不由撮合算术主导，而由**数据 I/O**主导，典型形态包括：

1. 策略或引擎大量调用 `get_price` / `get_current_data[security]`，形态为**单标的、`count=1`、逐只点查**。
2. `BacktestCurrentData.__getitem__` 原先**直接打 Provider**，绕过回测数据会话（`BacktestDataSession`）里的行情块缓存，导致会话开启后点查仍反复远程取数。
3. 撮合路径上「先读 `current_data`、再 `_resolve_base_exec_price` → `get_price`」形成**同 bar 双取价**。
4. 日终 `_update_positions` 对持仓**逐标的**取收盘价，持仓稍多即放大远程次数。
5. Tushare 日线前复权旧路径依赖 `ts.pro_bar(..., adj='qfq')`，在自定义代理上显著慢于 `daily` + `adj_factor`；且串行双 RTT 叠加延迟。

本分支在**不改变策略 API 语义**的前提下，对上述热路径做了针对性优化，并补充一套可复现的延迟基准脚本。

---

## 二、回测引擎热路径（`bullet_trade/core/engine.py`）

### 2.1 同 bar 成交价缓存 + hint

- 新增 `_bar_exec_price_cache`：键为 `(security, current_dt, fq_mode)`，避免同一根 bar 对同一标的重复解析成交价。
- `_resolve_base_exec_price` 增加 `hint_last_price`：在 `use_real_price` + 前复权场景下，若 `current_data.last_price` 已是可用前复权价，**直接用作成交基准价**，避免再打一次远程 `get_price`。
- 撮合下单时传入 `hint_last_price=security_data.last_price`，与 `current_data` 读取联动。

### 2.2 日终持仓估值批量化

- 新增 `_close_map_from_price_df`：从 `get_price(..., panel=False)` 的长表结果中按标的提取收盘价。
- `_update_positions`：对需要更新的持仓**批量**请求行情，再回填 `position.update_price`；单标的失败时再回退逐只取价，保证正确性。

**预期收益**：分钟线 / `every_bar`、多持仓策略上，远程 `get_price` 次数明显下降；沙箱对照中 stub 热路径曾从约数百秒量级降到十余秒（视策略与数据源而异）。

---

## 三、回测 CurrentData 走会话缓存（`bullet_trade/data/api.py`）

`BacktestCurrentData.__getitem__` 在构造点查参数后，**优先**调用 `_try_get_price_from_backtest_session(...)`（`count=1`、与原先相同的 frequency/fields/fq）。

- 会话命中：直接返回块内切片，不再打 Provider。
- 未命中 / 会话未启用：保持原 `_call_provider_get_price_with_security_fallback` 行为。

此前 `api.get_price()` 已会尝试会话，但策略里最常见的 `current_data[stock]` **没有**走同一路径，是小市值/打板类「候选池逐只过滤停牌」极慢的重要原因之一。本改动补齐该缺口。

---

## 四、数据会话默认开启行情块缓存

涉及文件：

- `bullet_trade/data/backtest_session.py`
- `bullet_trade/cli/backtest.py`
- `bullet_trade/cli/main.py`

行为变化：

1. **配置层**：`BacktestDataSessionConfig.from_env_and_overrides` 在会话 `enabled=True` 且未显式配置 `BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS` / `price_block_cache_enabled` 时，**默认打开** `price_block_cache_enabled`。
2. **CLI**：`--backtest-data-session` 启用时，写入的 `data_session_config` 将 `price_block_cache_enabled=True`（不再要求额外再开 `--backtest-price-block-cache` 才生效）。
3. Help 文案同步说明「开会话 ⇒ 默认行情块缓存」。

环境变量仍可覆盖：

| 变量 | 含义 |
|------|------|
| `BT_BACKTEST_DATA_SESSION` | 是否启用回测数据会话 |
| `BT_BACKTEST_DATA_SESSION_PRICE_BLOCKS` | 显式开关行情块缓存（设置后优先生效） |

---

## 五、Tushare Provider 增强（`bullet_trade/data/providers/tushare.py`）

本分支大幅扩展并优化 Tushare 适配层，使其更接近聚宽 `get_*` 表面，并降低日线前复权冷路径延迟。

### 5.1 `get_price` 日线前复权路径

- 股票日线优先 `pro.daily` 拉未复权 OHLCV，再用 `adj_factor` **本地复权**（避免慢的 `pro_bar adj=qfq`）。
- `daily` 与 `adj_factor` 通过 `ThreadPoolExecutor(max_workers=2)` **并行**，墙钟≈ `max(两路 RTT)`，而非串行相加。
- 本地 `_apply_adjustment_with_factor` 完成复权；尽量用区间末日作前复权基准，减少额外交易日历请求。
- 多标的场景下可并行拉取（线程池）；`count` 无 `start_date` 时用 `_estimate_start_for_count` 估算窗口，避免误拉全历史。

**实测量级（自定义代理冷启动，空磁盘缓存、`mem_cache=False`）**：

| 路径 | 约耗时 |
|------|--------|
| 旧：`pro_bar(..., adj='qfq')` | 约 5–8 s |
| 新：`daily ∥ adj_factor` / Provider `get_price` | 约 3 s |
| 同请求磁盘缓存命中 | 约数 ms |

说明：相对聚宽官方接口单次 `get_price` ~100ms，Tushare 冷启动仍受**代理 RTT**限制；优化消除的是 `pro_bar` 与串行双请求，不是把远端变成本地。

### 5.2 内存缓存开关

- 配置项 / 环境变量：`mem_cache` / `TUSHARE_MEM_CACHE`（bench 可设 `False` 测冷启动）。
- `stock_basic` 全表映射用于加速 `get_security_info` / 行业名等；**注意**：该映射当前走进程内 `_memo_call`，`mem_cache=False` 时每次都会重新打网，冷启动下 `get_security_info` 可能极慢（全表 `stock_basic`）。

### 5.3 对齐聚宽的 `get_*` 表面（节选）

在 Provider 上补齐/增强大量接口，供策略与基准脚本统一调用，例如：

`get_bars`、`get_ticks`、`get_current_tick`、`get_extras`、`get_fundamentals`、`get_fundamentals_continuously`、`get_industry` / `get_industry_stocks`、`get_concept` / `get_concept_stocks`、`get_fund_info`、融资融券列表、期货主力/合约、龙虎榜、限售解禁、`get_trade_day`、`get_live_current`、`get_split_dividend` 等。

基本面连续查询等路径对多日 `daily_basic` 使用线程池批量拉取。

### 5.4 ClickHouse 本地热读（可选）

- 客户端：`bullet_trade/data/providers/tushare_clickhouse.py`。
- 开启方式与 Docker 起库步骤见 **[docs/data/DATA_PROVIDER_TUSHARE.md](docs/data/DATA_PROVIDER_TUSHARE.md)**（本节不重复贴命令）。
- 本仓库只读本地库；表数据需外部 ETL 写入同一 ClickHouse（例如独立使用开源 tushare-integration，**不要**把该项目 vendoring 进本仓库）。
- 环境变量：`TUSHARE_CLICKHOUSE=true` + `HOST/PORT/...`，或 `TUSHARE_CLICKHOUSE_CONFIG=./config/tushare_clickhouse.example.yaml`。

### 5.5 其它小改动

- `bullet_trade/data/providers/base.py`：基类补充与 Provider 契约相关的小调整。
- `bullet_trade/data/cache.py`：缓存行为微调（配合 bench / Provider）。

---

## 六、延迟基准与剖析脚本

均在项目根目录、配置好 `.env`（数据源账号）后，用当前环境的 Python 运行。结果默认写在 `my_results/` 下对应子目录。

### 6.1 Provider 全接口耗时

| 脚本 | 说明 |
|------|------|
| `my_strategies/bench_jqdata_get_apis.py` | 聚宽 Provider 全部 `get_*` 耗时；可用 `BT_JQ_BENCH_*` 环境变量覆盖区间/样本数 |
| `my_strategies/bench_tushare_get_apis.py` | Tushare Provider 全部 `get_*` 耗时 |

Tushare 冷启动常用环境变量：

```text
BT_TS_BENCH_CACHE_DIR=<空目录>
BT_TS_BENCH_MEM_CACHE=0
BT_TS_BENCH_SAMPLES=1
```

输出示例：`my_results/tushare_api_latency/tushare_get_apis_latency.csv`  
（聚宽侧：`my_results/jqdata_api_latency/`）

### 6.2 回测 / 策略剖析

| 脚本 | 说明 |
|------|------|
| `my_strategies/bench_get_price_three_way.py` | jq 原生 vs 框架优化前/后三类对照（打板形态点查） |
| `my_strategies/bench_xiaoshizhi_profile.py` | 小市值策略分环节耗时 |
| `my_strategies/bench_ma5_single_profile.py` | 单股票均值策略分环节耗时 |
| `my_strategies/bench_daban_profile.py` | 打板策略剖析（注意聚宽日查询额度） |
| `my_strategies/bench_stub_get_price.py` | stub 行情下引擎热路径对照 |
| `my_strategies/daban_demo_for_bench.py` / `打板策略_bench_profile.py` | 打板剖析用策略变体 |

### 6.3 指数成分与阶段剖析（`my_results/index_daily/`）

| 脚本 | 说明 |
|------|------|
| `_fetch_hs300_constituents.py` 等 | 成分/日线拉取辅助 |
| `compare_jq_tushare_latency.py` | JQ vs Tushare 延迟对比 |
| `profile_backtest_stages.py` | 回测阶段耗时剖析 |

### 6.4 示例策略（便于对照）

- `my_strategies/mini_demo.py` / `mini_demo_index_universe.py`
- `my_strategies/小市值策略.py` / `单股票均值策略.py` / `打板策略.py`

---

## 七、延迟结论摘要（实测经验）

1. **回测慢**：多数情况下是「点查次数 × 单次远程延迟」，不是撮合公式；开启 `--backtest-data-session`（本分支会默认带行情块缓存）+ CurrentData 走会话后，重复点查收益最大。
2. **JQ `get_price` ~100ms vs Tushare 冷启动 ~3s**：主因是**数据源/代理链路 RTT**与「一次 RPC 带复权」vs「daily+adj 两路」，不是复权计算本身。Tushare 二次磁盘命中可到毫秒级。
3. **Tushare 冷启动最慢接口**常不是 `get_price`，而是首次全表 `stock_basic`（如 `get_security_info`），在 `mem_cache=False` 时尤其明显。
4. **策略侧**：`filter_paused_stock` 一类对候选列表逐只 `current_data[s]`，在未会话/未缓存时会把延迟放大到「候选数 × 调仓日数」量级。

---

## 八、如何运行（入口）

### 8.1 环境

```bash
# 建议使用独立虚拟环境或 conda env
pip install -e .
# 需要 Tushare / QMT 时按需：
# pip install -e ".[tushare]"
# pip install -e ".[qmt]"
```

在项目根目录配置 `.env`（可参考 `env.example` / `env.backtest.example`），至少包含所用数据源凭证，例如：

```env
DEFAULT_DATA_PROVIDER=jqdata   # 或 tushare
# JQDATA_USERNAME=...
# JQDATA_PASSWORD=...
# TUSHARE_TOKEN=...
# TUSHARE_CUSTOM_URL=...       # 可选自定义代理
# TUSHARE_CLICKHOUSE=true      # 可选：本地 ClickHouse 热读（见 docs/data/DATA_PROVIDER_TUSHARE.md）
# TUSHARE_CLICKHOUSE_HOST=127.0.0.1
# TUSHARE_CLICKHOUSE_PORT=8123
DATA_CACHE_DIR=~/.bullet-trade/cache
```

### 8.2 回测（推荐打开数据会话）

```bash
# 项目根目录
bullet-trade backtest my_strategies/mini_demo.py --start 2024-01-01 --end 2024-06-01 --backtest-data-session

# 或模块方式
python -m bullet_trade.cli.main backtest my_strategies/mini_demo.py --start 2024-01-01 --end 2024-06-01 --backtest-data-session
```

本分支下 `--backtest-data-session` 会默认启用内存行情块缓存。仅开块缓存、不开完整会话时仍可用：

```bash
bullet-trade backtest my_strategies/mini_demo.py --start 2024-01-01 --end 2024-06-01 --backtest-price-block-cache
```

### 8.3 延迟基准脚本

```bash
# 聚宽 get_* 耗时
python my_strategies/bench_jqdata_get_apis.py

# Tushare get_* 耗时（冷启动示例）
# Windows PowerShell:
#   $env:BT_TS_BENCH_CACHE_DIR="my_results/tushare_api_latency_cold/cache"
#   $env:BT_TS_BENCH_MEM_CACHE="0"
#   $env:BT_TS_BENCH_SAMPLES="1"
python my_strategies/bench_tushare_get_apis.py

# 策略/热路径剖析（按需）
python my_strategies/bench_xiaoshizhi_profile.py
python my_strategies/bench_get_price_three_way.py
```

### 8.4 实盘 / 更多文档

实盘、QMT、飞书通知等与主干相同，请直接查阅：

- [文档首页](docs/index.md) · https://bullettrade.cn/docs/
- [快速上手](docs/quickstart.md)
- [配置总览](docs/config.md)
- [回测引擎](docs/backtest.md)
- [数据源指南](docs/data/DATA_PROVIDER_GUIDE.md)

```bash
bullet-trade --help
bullet-trade live --help
```

---

**风险提示：** 量化交易存在高风险；延迟数字随网络、账号权限、代理与缓存状态变化，请以本机复现结果为准。
