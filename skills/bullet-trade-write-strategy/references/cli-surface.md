# BulletTrade CLI Surface

只记录框架公开的 CLI 入口与高频参数，足够支持常规策略开发、回测、优化、报告和运行。

## 全局入口

- 主命令：`bullet-trade`
- 全局参数：`--env-file <path>`
- 顶层子命令：`backtest`、`optimize`、`live`、`report`、`server`、`lab`、`jupyterlab`

## `backtest`

用途：运行单个策略回测并输出明细、图表和报告。

最小命令：

```bash
bullet-trade backtest strategy.py --start 2024-01-01 --end 2024-06-30
```

高频参数：

- `strategy_file`
- `--start YYYY-MM-DD`
- `--end YYYY-MM-DD`
- `--cash 100000`
- `--frequency day|minute`
- `--benchmark 000300.XSHG`
- `--output backtest_results/demo`
- `--log path/to/backtest.log`
- `--images`
- `--no-csv`
- `--no-html`
- `--no-logs`
- `--auto-report`
- `--report-format html|pdf`
- `--report-template path/to/template.html`
- `--report-metrics 策略收益,最大回撤,夏普比率`
- `--report-title 自定义标题`
- `--report-output path/to/report.html`
- `--backtest-data-session`
- `--backtest-data-session-manifest path/to/session_manifest.json`
- `--backtest-data-session-max-bytes 536870912`
- `--backtest-price-block-cache`

## `optimize`

用途：并行回测参数组合，输出排序后的 CSV。

```bash
bullet-trade optimize strategy.py \
  --params params.json \
  --start 2020-01-01 \
  --end 2024-12-31 \
  --processes 4 \
  --output optimization_results.csv
```

高频参数：

- `strategy_file`
- `--params params.json`
- `--start`
- `--end`
- `--processes`
- `--output`

## `live`

用途：启动策略实盘或仿真实盘执行。

```bash
bullet-trade live strategy.py --broker qmt
bullet-trade live strategy.py --broker qmt-remote --env-file .env.live
```

高频参数：

- `strategy_file`
- `--broker qmt|qmt-remote|simulator`
- `--log-dir`
- `--runtime-dir`

## `report`

用途：基于已有回测结果目录单独生成标准化报告。

```bash
bullet-trade report --input backtest_results/demo --format html --output reports/demo.html
```

高频参数：

- `--input` / `-i`
- `--output` / `-o`
- `--format html|pdf`
- `--template`
- `--metrics`
- `--title`

## `server`

用途：在 Windows + QMT 机器上启动远程数据/交易服务。

```bash
bullet-trade --env-file .env server \
  --listen 0.0.0.0 \
  --port 58620 \
  --enable-data \
  --enable-broker
```

高频参数：

- `--server-type`
- `--listen`
- `--port`
- `--token`
- `--tls-cert`
- `--tls-key`
- `--enable-data` / `--disable-data`
- `--enable-broker` / `--disable-broker`
- `--allowlist`
- `--max-connections`
- `--max-subscriptions`
- `--accounts`
- `--sub-accounts`
- `--log-file`
- `--log-account-overview`
- `--no-log-account-overview`
- `--access-log`
- `--no-access-log`

注意：

- 当前版本不支持 `--data-path`
- `QMT_DATA_PATH` 要写在 `.env`

## `lab` / `jupyterlab`

用途：启动研究环境。

```bash
bullet-trade lab
bullet-trade lab --diagnose
```

高频参数：

- `--ip`
- `--port`
- `--notebook-dir`
- `--no-browser`
- `--browser`
- `--token`
- `--no-token`
- `--password`
- `--certfile`
- `--keyfile`
- `--allow-origin`
- `--diagnose`
