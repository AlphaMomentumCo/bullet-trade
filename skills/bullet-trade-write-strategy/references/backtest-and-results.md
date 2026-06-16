# Backtest, Optimization, and Results

## 1. 最小回测

```bash
bullet-trade backtest strategy.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --benchmark 000300.XSHG \
  --cash 100000 \
  --output backtest_results/demo
```

推荐：

- 策略中开启 `set_option('use_real_price', True)`
- 若要避免未来函数，开启 `set_option('avoid_future_data', True)`

## 2. 常见输出文件

- `backtest.log`
- `report.html`
- `metrics.json`
- `daily_records.csv`
- `daily_positions.csv`
- `trades.csv`
- `annual_returns.csv`
- `monthly_returns.csv`
- `risk_metrics.csv`
- `open_counts.csv`
- `instrument_pnl.csv`
- `dividend_split_events.csv`

## 3. 结果判读

最常看这些指标：

- `策略收益`
- `策略年化收益`
- `最大回撤`
- `夏普比率`
- `索提诺比率`
- `Calmar 比率`
- `日胜率`
- `交易胜率`
- `盈亏比`

## 4. 自动报告

若用户要标准化报告：

```bash
bullet-trade backtest strategy.py \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --output backtest_results/demo \
  --auto-report \
  --report-format html
```

也可对已有结果目录单独生成：

```bash
bullet-trade report --input backtest_results/demo --format pdf
```

## 5. 参数优化

参数文件示例：

```json
{
  "param_grid": {
    "lookback": [10, 15, 20, 25, 30],
    "hold_days": [3, 5, 7, 10],
    "top_n": [1, 2, 3, 5]
  }
}
```

运行：

```bash
bullet-trade optimize strategy.py \
  --params params.json \
  --start 2018-01-01 \
  --end 2024-12-31 \
  --output optimization_results.csv
```

约定：

- `param_grid` 的键必须对应策略中的 `g.xxx`
- 排序核心指标默认是收益回撤比（Calmar）

## 6. 回测数据会话优化

适合长区间、重复拉历史行情的回测：

```bash
bullet-trade backtest strategy.py \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --backtest-data-session \
  --backtest-data-session-manifest backtest_results/session_manifest.json
```

可选：

- `--backtest-price-block-cache`
- `--backtest-data-session-max-bytes`

## 7. 仓库内回归验证

若任务是在本仓库里为策略建立回归测试，可使用：

```bash
pytest tests/test_strategies.py -v -s
pytest tests/test_strategies.py -v -s -k "my_strategy"
```

约定：

- 策略文件放 `tests/strategies/`
- 配置放 `tests/strategies/config.yaml`
- 策略文件仍保持 `from jqdata import *`
