# Tushare 数据源封装说明

`TushareProvider` 位于 `bullet_trade/data/providers/tushare.py`，通过 `DEFAULT_DATA_PROVIDER=tushare` 或 `set_data_provider('tushare', token='xxx')` 激活。

## 安装与认证
- 依赖 `tushare>=1.2.0`，建议通过 `pip install bullet-trade[tushare]` 一键安装。  
- 认证优先级：`set_data_provider` 参数 > `.env` 中的 `TUSHARE_TOKEN` > 构造函数传入。  
- Provider 会在首次调用时自动创建 `ts.pro_api` 客户端，并将 `cache_dir` 设置为 `DATA_CACHE_DIR/tushare`（若配置）。
- 如需自定义接入点，可在 `.env` 中设置 `TUSHARE_CUSTOM_URL`，或在 `set_data_provider('tushare', tushare_custom_url='...')` 中传入。

## 价格获取策略
- 始终获取未复权行情 + 复权因子，自行计算前/后复权并应用 `pre_factor_ref_date`。  
- **前复权锚点**：未传 `pre_factor_ref_date` 时锚定**最新交易日**（与聚宽一致），而非查询区间末日；成交量按价格比例反权重，成交额保持不变。  
- 支持 `frequency` 为日线 (`D`) / 多个分钟级别 (`1min`、`5min`等)，与聚宽接口保持一致。  
- 支持聚宽代码后缀自动转换，并按证券类型选择 Tushare 行情资产：股票 `asset='E'`、指数 `asset='I'`、基金/ETF `asset='FD'`。  
- 北交所：`BJ/BSE` ↔ `XBEI`；老码 `821/43/83/87` 映射为 `920` 新码。
- `skip_paused=True` 时依据 `is_paused` 字段过滤；若缺失则全部保留。  
- 多标的请求会拆分为多个单标的调用，并在返回时根据 `panel` 参数拼接（`panel=True` 为列 MultiIndex，`panel=False` 输出长表）。

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
1. **速率限制**：Pro 账号默认 5000 次/分钟，如高频调用建议开启 `DATA_CACHE_DIR` 缓存目录或在私有网络中本地化数据。  
2. **数据完整性**：部分场外基金/LOF 在 `fund_basic` 中缺少 `delist_date`，封装会将其解析为 `NaT`，可在策略端自行填补。  
3. **资产类型判断**：常见股票/指数/ETF 代码会先通过后缀和前缀快速判断；无法确定时回退到 `index_basic` / `fund_basic` / `stock_basic` 目录查询。
4. **分钟线权限**：若账号未开通分钟级别数据，`ts.pro_bar` 会返回空 DataFrame；框架会在日志层面记录，策略需自行兜底。
5. **北交所口径**：`ts` 侧 `types=stock` 默认不含北交所；`jq` 的 `get_all_securities` / 融资标的通常也不含。若显式 `include_bse=True`，代码为 `xxxx.XBEI`（920 新码）。

总体而言，TushareProvider 在无需依赖聚宽账号的情况下提供了等价的 API 行为，并支持动态复权与标准化分红事件，是纯离线或学术环境的推荐选择。
