# BulletTrade Strategy Authoring

## 总原则

- 默认写成聚宽兼容风格：`from jqdata import *`
- 尽量让策略只依赖统一 API，方便在 `jqdata`、`qmt`、`qmt-remote`、`tushare` 之间切换
- 参数优先放在 `g.xxx`
- 盘前、盘中读取价格时，优先用 `context.previous_date` 或历史窗口，减少未来函数风险
- 如果用户明确要求使用 `tushare`，优先复用 `strategy-examples-tushare.md` 里的样例，并限制数据 API 在 Tushare 已支持的范围内

## 最小策略模板

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)

    g.stock_pool = ['000001.XSHE', '600000.XSHG']
    g.target_value = 10000

    run_daily(rebalance, time='10:00')


def rebalance(context):
    current_data = get_current_data()

    for stock in g.stock_pool:
        if current_data[stock].paused:
            continue
        order_target_value(stock, g.target_value)
```

## 常用写法

### 1. 盘前准备 + 开盘交易

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    g.ma_period = 5
    g.stock_pool = []

    run_daily(before_open, time='before_open')
    run_daily(market_open, time='open')


def before_open(context):
    g.stock_pool = get_index_stocks('000300.XSHG')[:10]


def market_open(context):
    for stock in g.stock_pool:
        df = get_price(
            stock,
            end_date=context.previous_date,
            count=g.ma_period + 1,
            fields=['close'],
        )
        if df is None or len(df) < g.ma_period + 1:
            continue

        ma = df['close'].rolling(g.ma_period).mean().iloc[-1]
        close = df['close'].iloc[-1]
        if close > ma:
            order_value(stock, context.portfolio.total_value * 0.1)
```

### 2. 用 `g.xxx` 为优化做准备

```python
from jqdata import *


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)

    g.lookback = 20
    g.top_n = 3
    g.hold_days = 5

    run_daily(trade, time='10:00')
```

`bullet-trade optimize` 会在 `initialize()` 后覆盖同名 `g.xxx` 参数。

### 3. provider 直连模板

只在统一 API 不够时使用：

```python
from jqdata import *
from bullet_trade.data.api import get_data_provider


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    g.jq = get_data_provider("jqdata")
```

适用场景：

- 基础面
- `get_extras`
- 某 provider 独有字段或 SDK 能力

代价：

- 策略会被锁定到该 provider

## Tushare 样例入口

当用户要“直接给我几个可回测策略样例”，并且要求只使用 Tushare 已支持的数据 API 时：

- 直接读 `strategy-examples-tushare.md`
- 优先复用那里的模板，不要临时发明基础面接口
- 样例里只使用这些数据 API：
  - `get_price`
  - `attribute_history`
  - `get_current_data`
  - `get_trade_days`
  - `get_all_securities`
  - `get_index_stocks`
  - `get_split_dividend`

## 写策略时的优先级

1. 先确定任务是“纯价格量能策略”还是“依赖基础面 / 特有接口”
2. 纯价格量能策略优先只用统一 API
3. 基础面策略先确认 provider，必要时显式说明只支持 `jqdata`
4. live 恢复相关逻辑放 `process_initialize(context)`

## 常见坑

- 不要默认当日 `close/high/low` 在盘前或盘中可安全读取
- 不要把 provider 特有接口当成统一 API
- 不要把 live 必需动作只放进 `initialize(context)`；重启恢复时它可能不再执行
- 不要在需要跨数据源时写死 QMT 格式代码；优先 `000001.XSHE` 这种聚宽格式
