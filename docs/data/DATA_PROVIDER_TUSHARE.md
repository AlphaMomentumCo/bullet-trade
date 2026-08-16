# Tushare 数据源封装说明

`TushareProvider` 位于 `bullet_trade/data/providers/tushare.py`，通过 `DEFAULT_DATA_PROVIDER=tushare` 或 `set_data_provider('tushare', token='xxx')` 激活。

## 安装与认证
- 依赖 `tushare>=1.2.0`，建议通过 `pip install bullet-trade[tushare]` 一键安装。  
- 认证优先级：`set_data_provider` 参数 > `.env` 中的 `TUSHARE_TOKEN` > 构造函数传入。  
- Provider 会在首次调用时自动创建 `ts.pro_api` 客户端，并将 `cache_dir` 设置为 `DATA_CACHE_DIR/tushare`（若配置）。
- 如需自定义接入点，可在 `.env` 中设置 `TUSHARE_CUSTOM_URL`，或在 `set_data_provider('tushare', tushare_custom_url='...')` 中传入。
- 可选本地热链路：`pip install clickhouse-connect`（见 `requirements.txt` 注释），配合下方 Docker ClickHouse。

## 价格获取策略
- 始终获取未复权行情 + 复权因子，自行计算前/后复权并应用 `pre_factor_ref_date`。  
- **前复权锚点**：未传 `pre_factor_ref_date` 时锚定**最新交易日**（与聚宽一致），而非查询区间末日；成交量按价格比例反权重，成交额保持不变。  
- 有 ClickHouse 时：日线优先本地 `daily` + `adj_factor`（或 JOIN），未命中再回退远程。  
- 支持 `frequency` 为日线 (`D`) / 多个分钟级别 (`1min`、`5min`等)，与聚宽接口保持一致。  
- 支持聚宽代码后缀自动转换，并按证券类型选择 Tushare 行情资产：股票 `asset='E'`、指数 `asset='I'`、基金/ETF `asset='FD'`。  
- 北交所：`BJ/BSE` ↔ `XBEI`；老码 `821/43/83/87` 映射为 `920` 新码。
- `skip_paused=True` 时依据 `is_paused` 字段过滤；若缺失则全部保留。  
- 多标的请求会拆分为多个单标的调用，并在返回时根据 `panel` 参数拼接（`panel=True` 为列 MultiIndex，`panel=False` 输出长表）。

## Docker + ClickHouse 热链路（推荐本地读）

本仓库**只负责读**：`TushareClickHouseClient`（`bullet_trade/data/providers/tushare_clickhouse.py`）连本地 ClickHouse，未命中回退远程 Tushare API。  
**写库 / 同步**不在本仓库内；可用独立开源工具 [tushare-integration](https://github.com/zhangbc97/tushare-integration)（Docker 镜像 `zhangbc/tushare-integration`）或自有 ETL，把 `daily` / `adj_factor` / `trade_cal` / `stock_basic` 等表写入同一 ClickHouse。

### 数据链路

```text
Tushare API
    │  写：外部 ETL / tushare-integration spider（本仓库不含）
    ▼
ClickHouse (Docker, :8123)
    │  读：TushareProvider → TushareClickHouseClient
    │      优先 bt_*_fast 窄表；未命中 → 远程 API
    ▼
策略 / 回测
```

### 1. 启动 ClickHouse（一次性）

Windows 建议：WSL2 + Docker Desktop（勾选 WSL Integration）。Docker Desktop 下用端口映射，勿依赖 `--net=host`：

```bash
wsl
mkdir -p ~/data/clickhouse
docker rm -f clickhouse-server 2>/dev/null || true

docker pull clickhouse/clickhouse-server:23.6.2.18-alpine
docker run -d \
  --name clickhouse-server \
  -p 8123:8123 \
  -p 9000:9000 \
  -v ~/data/clickhouse:/var/lib/clickhouse \
  --ulimit nofile=262144:262144 \
  clickhouse/clickhouse-server:23.6.2.18-alpine

docker ps | grep clickhouse
docker exec -it clickhouse-server clickhouse-client --query "SELECT 1"
```

容器名若不同，后续 `docker exec` 换成实际名称；只要 `8123` 已映射，bullet-trade 仍连 `127.0.0.1:8123`。

### 2. 在 bullet-trade 中开启热读

`.env`（见 `env.example`）：

```env
DEFAULT_DATA_PROVIDER=tushare
TUSHARE_TOKEN=your_token_here
# TUSHARE_CUSTOM_URL=https://your-proxy/api

TUSHARE_CLICKHOUSE=true
TUSHARE_CLICKHOUSE_HOST=127.0.0.1
TUSHARE_CLICKHOUSE_PORT=8123
TUSHARE_CLICKHOUSE_USER=default
TUSHARE_CLICKHOUSE_PASSWORD=
TUSHARE_CLICKHOUSE_DB=default
# 可选：指向含 database: 段的 YAML（示例见 config/tushare_clickhouse.example.yaml）
# TUSHARE_CLICKHOUSE_CONFIG=./config/tushare_clickhouse.example.yaml

# 读优化（默认即推荐）
TUSHARE_CLICKHOUSE_FINAL=false
TUSHARE_CLICKHOUSE_FAST=true
TUSHARE_CLICKHOUSE_AUTO_FAST=true
```

连通自检（项目根目录）：

```bash
pip install clickhouse-connect
python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env", override=True)
from bullet_trade.data.providers.tushare_clickhouse import build_clickhouse_client
ch = build_clickhouse_client({})
assert ch and ch.is_available(), "ClickHouse 不可用"
print(ch.query_df("SELECT 1 AS ok"))
print(ch.query_df("SHOW TABLES"))
PY
```

HTTP 快速核对：

```bash
curl "http://127.0.0.1:8123/?query=SHOW%20TABLES"
# 有密码时加 &user=default&password=...
```

### 3. 写库（外部，可选）

表需先有数据，Provider 才能本地命中。可用官方文档中的 Docker 镜像或自行仓库，例如：

```bash
# 示例：独立克隆 tushare-integration（勿放入本仓库）
# 配置其 config.yaml 指向同一 127.0.0.1:8123 后：
# python main.py run spider stock/basic/stock_basic
# python main.py run spider stock/quotes/daily
# python main.py run spider stock/quotes/adj_factor
# python main.py run spider stock/basic/trade_cal
```

写入后验收：

```bash
docker exec -it clickhouse-server clickhouse-client --query "SHOW TABLES"
docker exec -it clickhouse-server clickhouse-client --query "SELECT count() FROM daily"
```

### 4. 读优化说明（本仓库内建）

| 项 | 行为 |
|----|------|
| `TUSHARE_CLICKHOUSE_FAST` | 优先读 `bt_*_fast` MergeTree 投影表（无 FINAL） |
| `TUSHARE_CLICKHOUSE_AUTO_FAST` | 启动时自动建/灌空的 fast 表 |
| `TUSHARE_CLICKHOUSE_FINAL` | 源表 ReplacingMergeTree 是否加 `FINAL`（大表默认关） |

常用源表：`daily`、`adj_factor`、`trade_cal`、`stock_basic`、`daily_basic`、`index_weight`、`fund_basic` 等。

### 5. 运维与排障

```bash
docker start clickhouse-server
docker stop clickhouse-server
docker logs -f clickhouse-server
# 数据目录：~/data/clickhouse（或你挂载的路径）
```

| 现象 | 处理 |
|------|------|
| `docker: command not found` | 开 Docker Desktop，勾选 WSL Integration |
| 连不上 `127.0.0.1:8123` | 查 `docker ps` 端口映射 |
| Provider 仍打远程 | 确认 `TUSHARE_CLICKHOUSE=true` 且表有数据；看日志「热链路已连接」 |
| 表不存在 / `count()=0` | 先完成外部写库，再读 |
| HTTP curl 无输出 | 密码与 env 一致，或改用 Python / `clickhouse-client` |

## 分红与拆分
- 调用 `pro.dividend`，将 `cash_div`、`stock_div`、`stock_transfer` 映射为标准化事件：  
  `scale_factor = 1 + (stock_div + stock_transfer) / 10`，`bonus_pre_tax = cash_div`，`per_base=10`。  
- 若区间内无数据，返回空列表，框架会自动跳过该证券的事件处理。

## 指数与基础信息
- `get_all_securities` 合并 `stock_basic` / `fund_basic` / `index_basic` 等接口，统一产出 `display_name`/`name`/`start_date`/`end_date`/`type`。  
  - `types=stock` 且传入 `date`：拉取 `list_status=L+D`（必要时补 `P`），再按 `list_date`/`delist_date` 过滤。  
  - `types=stock` 且 `date` 为空：仅 `L`（当前在市）。  
  - **默认剔除北交所**（与聚宽对齐）；需要北交所时设 `include_bse=True` 或环境变量 `TUSHARE_INCLUDE_BSE=1`。  
- `get_margincash_stocks` / `get_marginsec_stocks` 同源 `margin_secs`；返回码已做北交所老→新映射。与聚宽比对时**不要期待北交所一致**（jq 通常不含 BSE）。  
- `get_index_stocks` 使用 `index_weight`，默认取查询日期或当前交易日所在月的数据。  
- 交易日来源于 `trade_cal(exchange='SSE')`，只保留 `is_open=1` 的记录。

## 使用提示
1. **速率限制**：Pro 账号默认 5000 次/分钟；高频回测建议开磁盘缓存或 ClickHouse 热链路。  
2. **数据完整性**：部分场外基金/LOF 在 `fund_basic` 中缺少 `delist_date`，封装会将其解析为 `NaT`，可在策略端自行填补。  
3. **资产类型判断**：常见股票/指数/ETF 代码会先通过后缀和前缀快速判断；无法确定时回退到 `index_basic` / `fund_basic` / `stock_basic` 目录查询。
4. **分钟线权限**：若账号未开通分钟级别数据，`ts.pro_bar` 会返回空 DataFrame；框架会在日志层面记录，策略需自行兜底。
5. **北交所口径**：`ts` 侧 `types=stock` 默认不含北交所；`jq` 的 `get_all_securities` / 融资标的通常也不含。若显式 `include_bse=True`，代码为 `xxxx.XBEI`（920 新码）。

总体而言，TushareProvider 在无需依赖聚宽账号的情况下提供了等价的 API 行为，并支持动态复权、可选本地 ClickHouse 热读与标准化分红事件。
