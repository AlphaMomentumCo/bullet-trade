---
name: tushare-integration-first
description: >-
  Prefer optimizing tushare-integration (ETL, CH materialize, local_tushare_proxy hot read)
  over bullet-trade for Tushare/ClickHouse data-plane latency and schema work. Use when
  changing get_price hot path, bt_*_fast, proxy, CH sync, or comparing CH-direct vs proxy.
  Keep commits split: never commit nested tushare-integration/ from bullet-trade.
---

# Tushare 数据面：优先改 tushare-integration

## 原则

1. **数据面（表结构、物化、代理读、批量 SQL、序列化）** → 优先在 `tushare-integration`（尤其 `dev/table_delay_opt_*`）改，并提交到 **AlphaMomentumCo/tushare-integration** 对应分支。
2. **策略面（`get_price` 语义、jq↔ts、复权锚点、panel、回退远程、批量 HTTP）** → 留在 `bullet-trade`，提交到 **bullet-trade** 仓库。
3. 不要把 `ensure_fast_tables` / DDL 再绑回策略进程默认路径；物化用 `python main.py fast materialize`。

## 仓库与提交隔离（强制）

- 工作区里的 `tushare-integration/` 是**独立 git 仓库**（嵌套目录），**禁止**从 `bullet-trade` 的 `git add` / commit 带入该目录。
- bullet-trade 只提交策略适配相关文件（如 `tushare.py`、`tushare_clickhouse.py`、bench 脚本、本 skill）；勿整仓盲提。
- tushare-integration 只提交数据面相关 py/yaml（proxy、fast、commands）；**勿提交**含 token/密码的 `config.yaml`。
- 两仓各自 commit / push，commit message 写清动机、改动点、优化结果。

## 已知慢因（proxy vs CH 直连）

CH 直连：`get_price` → 一次/分块 `IN (...)` 查 `bt_daily_adj_fast`（宽表、PREWHERE、列裁剪）。

Proxy 旧路径：关 CH 后 **按票** `_get_price_single` → 每票多次 HTTP（daily + adj_factor，还可能试 pro_bar）→ proxy `query_df` + **按行 itertuples** 拼 JSON。

因此 10 票约慢 **25×** 的主因是 **RTT×N + JSON 行式序列化**，不是 fast 表本身失效。

## 优化优先级（integration）

1. 多 `ts_code` 单次查询（逗号/`IN`）+ `bt_daily_adj_fast` 一次出 OHLCV+adj  
2. CH 用列式取回；JSON 用列向量化，禁止大结果集逐行 Python 循环  
3. `prefer_fast` / 禁 FINAL / PREWHERE（已有则保持）  
4. 提高批量场景 `default_limit`/`max_limit`，避免截断；全市场扫描勿静默 50k LIMIT

## bullet-trade 仅做薄适配

- 指向本地 proxy 时，**批量** `pro.daily(ts_code="a,b,c", ...)`（可带 `adj_factor` 字段走宽表），不要无 CH 就退回逐票。  
- 勿默认 `AUTO_FAST=1` 在策略里建表。

## 验证

对比同窗口、同 N 票：`CH 直连 hot_p50` vs `integration_proxy hot_p50`；结果可写 `my_results/index_universe_link_latency/`。
