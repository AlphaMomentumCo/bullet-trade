# Tushare Strategy Examples

本文件提供几个可直接复用的策略样例。

硬约束：

- 数据源假定为 `tushare`
- 数据 API 只使用 Tushare 已支持的接口
- 不使用 `get_fundamentals`、`query`、`get_extras` 等接口

建议准备：

```env
DEFAULT_DATA_PROVIDER=tushare
TUSHARE_TOKEN=your_token
```

如果想在策略内显式指定：

```python
from bullet_trade.data.api import set_data_provider

set_data_provider('tushare')
```

## 可用数据 API 白名单

下面样例只使用这些数据接口：

- `get_price`
- `attribute_history`
- `get_current_data`
- `get_trade_days`
- `get_all_securities`
- `get_index_stocks`
- `get_split_dividend`

## 样例 1：单股票均线择时

特点：

- 最简单
- 适合验证 Tushare 环境是否跑通
- 只依赖 `attribute_history` 和 `get_current_data`

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    g.security = '000001.XSHE'
    g.ma_period = 20
    g.target_ratio = 0.9

    run_daily(trade, time='10:00')


def trade(context):
    hist = attribute_history(
        g.security,
        g.ma_period + 1,
        unit='1d',
        fields=['close'],
        skip_paused=True,
        df=True,
        fq='pre',
    )
    if hist is None or len(hist) < g.ma_period + 1:
        return

    ma = hist['close'].rolling(g.ma_period).mean().iloc[-1]
    last_close = hist['close'].iloc[-1]
    snap = get_current_data()[g.security]

    if snap.paused:
        return

    if last_close > ma:
        order_target_value(g.security, context.portfolio.total_value * g.target_ratio)
    else:
        order_target_value(g.security, 0)
```

使用的数据 API：

- `attribute_history`
- `get_current_data`

## 样例 2：沪深 300 成分股动量轮动

特点：

- 使用指数成分股作为股票池
- 用最近 N 日涨幅排序
- 每周调仓一次

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    g.index_code = '000300.XSHG'
    g.lookback = 20
    g.top_n = 5

    run_weekly(rebalance, 1, time='10:00')


def rebalance(context):
    stocks = get_index_stocks(g.index_code, date=context.previous_date)
    if not stocks:
        return

    price_df = get_price(
        stocks,
        end_date=context.previous_date,
        count=g.lookback + 1,
        frequency='daily',
        fields=['close'],
        panel=False,
        fq='pre',
    )
    if price_df is None or price_df.empty:
        return

    momentum = {}
    for code, group in price_df.groupby('code'):
        closes = group['close'].dropna()
        if len(closes) < g.lookback + 1:
            continue
        momentum[code] = closes.iloc[-1] / closes.iloc[0] - 1

    if not momentum:
        return

    targets = [code for code, _ in sorted(momentum.items(), key=lambda x: x[1], reverse=True)[:g.top_n]]
    current_positions = list(context.portfolio.positions.keys())

    for stock in current_positions:
        if stock not in targets:
            order_target_value(stock, 0)

    per_value = context.portfolio.total_value / len(targets)
    current_data = get_current_data()
    for stock in targets:
        if current_data[stock].paused:
            continue
        order_target_value(stock, per_value)
```

使用的数据 API：

- `get_index_stocks`
- `get_price`
- `get_current_data`

## 样例 3：全市场低波动 ETF 轮动

特点：

- 不依赖基础面
- 从 `get_all_securities` 中筛 ETF
- 选择最近一段时间波动率最低的品种

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    g.lookback = 30
    g.max_hold = 3

    run_monthly(rebalance, 1, time='10:00')


def rebalance(context):
    all_funds = get_all_securities(types='fund', date=context.previous_date)
    if all_funds is None or all_funds.empty:
        return

    etf_codes = []
    for code in all_funds.index.tolist():
        upper_code = str(code).upper()
        if upper_code.endswith('.XSHG') or upper_code.endswith('.XSHE'):
            etf_codes.append(code)
    etf_codes = etf_codes[:50]
    if not etf_codes:
        return

    price_df = get_price(
        etf_codes,
        end_date=context.previous_date,
        count=g.lookback,
        fields=['close'],
        panel=False,
        fq='pre',
    )
    if price_df is None or price_df.empty:
        return

    score = {}
    for code, group in price_df.groupby('code'):
        closes = group['close'].dropna()
        if len(closes) < g.lookback:
            continue
        returns = closes.pct_change().dropna()
        if returns.empty:
            continue
        score[code] = returns.std()

    if not score:
        return

    targets = [code for code, _ in sorted(score.items(), key=lambda x: x[1])[:g.max_hold]]
    current_positions = list(context.portfolio.positions.keys())

    for stock in current_positions:
        if stock not in targets:
            order_target_value(stock, 0)

    per_value = context.portfolio.total_value / len(targets)
    for stock in targets:
        order_target_value(stock, per_value)
```

使用的数据 API：

- `get_all_securities`
- `get_price`

## 样例 4：分红事件后的持有策略

特点：

- 演示如何用 `get_split_dividend`
- 适合做事件驱动回测模板

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    g.security = '600000.XSHG'
    g.hold_days = 5
    g.entry_date = None

    run_daily(trade, time='10:00')


def trade(context):
    today = context.previous_date
    events = get_split_dividend(
        g.security,
        start_date=today,
        end_date=today,
    )

    has_dividend_event = bool(events)
    has_position = g.security in context.portfolio.positions

    if has_dividend_event and not has_position:
        order_target_value(g.security, context.portfolio.total_value * 0.9)
        g.entry_date = today
        return

    if has_position and g.entry_date is not None:
        trade_days = get_trade_days(start_date=g.entry_date, end_date=today)
        if len(trade_days) >= g.hold_days:
            order_target_value(g.security, 0)
            g.entry_date = None
```

使用的数据 API：

- `get_split_dividend`
- `get_trade_days`

## 样例改写规则

当需要基于这些样例生成新策略时：

1. 先保留样例骨架和调度方式
2. 只替换股票池、排序逻辑、参数名
3. 不要把基础面接口掺进来
4. 若需要更多筛选条件，优先从价格、成交额、交易日、成分股、分红事件这几类信息组合
