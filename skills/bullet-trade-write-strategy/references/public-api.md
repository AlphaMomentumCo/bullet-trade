# BulletTrade Public Strategy API

默认策略导入方式：

```python
from jqdata import *
```

这里只写框架公开出来、可直接依赖的策略 API，不写内部实现。

## 1. 全局对象与消息

### `g`

全局状态容器，可挂任意可序列化属性。

- 用途：存放参数、计数器、缓存结果、选股池
- 生命周期：回测开始时重置；live 可随运行态持久化
- 示例：`g.lookback = 20`

### `log`

日志对象，支持：

- `log.debug(message)`
- `log.info(message)`
- `log.warn(message)`
- `log.error(message)`
- `log.critical(message)`

### `log.set_level(module, level)`

- `module`：`system` 或 `strategy`
- `level`：如 `DEBUG`、`INFO`、`WARN`、`ERROR`
- 用途：调整日志输出级别

### `send_msg(message)`

- `message`：字符串消息内容
- 行为：输出策略消息日志；若配置企业微信机器人，会尝试推送

### `set_message_handler(handler)`

- `handler`：可调用对象，签名通常为 `handler(message: str)`；传 `None` 表示清除
- 用途：自定义消息投递逻辑

## 2. 生命周期函数

### `initialize(context)`

- `context`：运行上下文对象
- 用途：初始化基准、参数、调度、股票池、风控配置
- 时机：回测启动时执行；live 首次启动执行，恢复运行态时可能跳过

### `process_initialize(context)`

- `context`：运行上下文对象
- 用途：执行必须在 live 环境就绪后再做的动作，如恢复订阅、同步账号、打印实盘状态
- 时机：仅 live

### `before_trading_start(context)`

- `context`：运行上下文对象
- 用途：开盘前准备，如更新股票池、加载前一日数据

### `after_trading_end(context)`

- `context`：运行上下文对象
- 用途：收盘后统计、记录结果、推送摘要

### `handle_data(context, data)`

- `context`：运行上下文对象
- `data`：行情数据容器
- 用途：每个 bar 执行交易逻辑
- 时机：按回测频率或 live 调度触发

### `after_code_changed(context)`

- `context`：运行上下文对象
- 用途：代码热更新后执行修复动作
- 时机：仅 live

重要边界：

- `initialize(context)` 不能假定每次 live 进程重启都会执行
- “每次 live 启动都必须执行”的动作优先放进 `process_initialize(context)`

## 3. 调度接口

### `run_daily(func, time='every_bar')`

- `func`：要执行的函数
- `time`：触发时间表达式

常用 `time`：

- `every_bar`：每个 bar 执行
- `every_minute`：等价于 `every_bar`
- `open`：开盘
- `before_open`：开盘前 30 分钟
- `after_close`：收盘后 30 分钟
- `'10:00'`：指定时刻
- `open-30m`、`close+10s`：相对开收盘时间

### `run_weekly(func, weekday, time='09:30', reference_security=None, force=True)`

- `func`：要执行的函数
- `weekday`：当周第几个交易日；支持负数，`-1` 表示最后一个交易日
- `time`：触发时间，写法同 `run_daily`
- `reference_security`：用于判定交易日和交易时段的参考标的；通常可留空
- `force`：
  - `True`：首周或不足周时尽量补跑
  - `False`：严格按自然周交易日计算，错过不补

### `run_monthly(func, monthday, time='09:30', reference_security=None, force=True)`

- `func`：要执行的函数
- `monthday`：当月第几个交易日；支持负数，`-1` 表示最后一个交易日
- 其余参数与 `run_weekly` 含义一致

### `unschedule_all()`

- 用途：清空当前策略已注册的所有定时任务

## 4. 设置与风控

### `set_benchmark(security)`

- `security`：基准代码，如 `000300.XSHG`
- 用途：设置回测和报告对比基准

### `set_option(key, value)`

只支持框架实现过的 key。

常用键：

- `use_real_price`
  - `value`：`True` / `False`
  - 用途：开启真实价格 / 动态复权撮合
- `avoid_future_data`
  - `value`：`True` / `False`
  - 用途：阻止在盘前/盘中读取未来字段
- `order_volume_ratio`
  - `value`：`0` 到 `1`
  - 用途：限制撮合时允许成交的量占比
- `order_match_mode`
  - `value`：`'immediate'` 或 `'bar_end'`
  - 用途：设置下单后立即撮合还是 bar 结束撮合
- `match_by_signal`
  - `value`：`True` / `False`
  - 用途：限价单资金检查是使用信号价还是撮合价
- `market_period`
  - `value`：自定义交易时段配置
  - 用途：覆盖默认交易时段
- `time_aliases`
  - `value`：时间别名字典
  - 用途：覆盖 `before_open` 等别名

### `set_slippage(slippage, type=None, ref=None)`

- `slippage`：如 `FixedSlippage(...)`、`PriceRelatedSlippage(...)`、`StepRelatedSlippage(...)`
- `type`：标的类别，如 `stock`
- `ref`：特定证券代码；优先级高于 `type`
- 用途：设置滑点模型

### `set_order_cost(cost, type='stock', ref=None)`

- `cost`：`OrderCost(...)` 对象
- `type`：证券类别，默认 `stock`
- `ref`：可选，指定单个证券代码覆盖默认费率
- 用途：设置佣金、印花税、最小佣金

### `set_commission(per_trade)`

- `per_trade`：`PerTrade(...)`
- 用途：聚宽兼容股票费率设置

### `set_universe(stocks)`

- `stocks`：证券代码列表、元组或可迭代对象
- 用途：记录策略标的池

### `set_data_provider(name_or_instance, **kwargs)`

- `name_or_instance`：provider 名称或 provider 实例
- 内置名称：`jqdata`、`tushare`、`qmt`、`miniqmt`、`qmt-remote`
- `**kwargs`：provider 初始化参数
- 用途：切换当前数据源

### `get_data_provider(name=None)`

- `name`：可选；不传时取当前默认 provider，传名称时返回指定 provider 实例
- 用途：读取当前 provider，或按名称直连特定 provider

## 5. 数据接口

### `get_price(security, start_date=None, end_date=None, frequency='daily', fields=None, skip_paused=False, fq='pre', count=None, panel=True, fill_paused=True)`

用途：获取单标的或多标的历史行情。

参数：

- `security`
  - 单个代码或代码列表
  - 例：`'000001.XSHE'`、`['000001.XSHE', '600000.XSHG']`
- `start_date`
  - 起始时间，可为字符串或日期时间对象
  - 与 `count` 二选一或组合使用
- `end_date`
  - 结束时间，可为字符串或日期时间对象
  - 回测中会自动截断到当前回测时点
- `frequency`
  - `daily`、`1d`、`minute`、`1m`
- `fields`
  - 要返回的字段列表
  - 常用：`open`、`close`、`high`、`low`、`volume`、`money`、`high_limit`、`low_limit`、`paused`
- `skip_paused`
  - `True`：跳过停牌
  - `False`：保留停牌行
- `fq`
  - 复权方式，常见为 `pre`
- `count`
  - 返回最近多少条记录
- `panel`
  - 多标的时是否返回聚宽兼容结构
- `fill_paused`
  - 是否填充停牌数据

返回：

- 单标的一般返回 DataFrame
- 多标的一般返回聚宽兼容结构；当前框架会尽量保持 `df['close']` 可直接取出二维矩阵

注意：

- `avoid_future_data=True` 时，盘中读取未来字段会报错
- `use_real_price=True` 会影响前复权参考日

### `attribute_history(security, count, unit='1d', fields=None, skip_paused=False, df=True, fq='pre')`

用途：取某个标的最近 N 条历史数据。

参数：

- `security`：单个证券代码
- `count`：返回条数
- `unit`：`1d`、`1m` 等
- `fields`：字段列表
- `skip_paused`：是否跳过停牌
- `df`：
  - `True`：返回 DataFrame
  - `False`：返回更底层结构
- `fq`：复权方式

注意：

- 该接口底层仍走 `get_price`
- 回测里会自动偏移结束时间，尽量避免未来函数

### `get_current_data()`

用途：获取当前行情快照容器。

返回：

- 可通过 `current_data[code]` 访问单标的快照

稳定可用字段：

- `last_price`
- `high_limit`
- `low_limit`
- `paused`

不要假定存在所有聚宽字段，如 `day_open`

### `get_trade_days(start_date=None, end_date=None, count=None)`

用途：获取交易日列表。

参数：

- `start_date`：起始日期
- `end_date`：结束日期
- `count`：最近多少个交易日

返回：

- 交易日列表

### `get_all_securities(types='stock', date=None)`

用途：获取标的信息表。

参数：

- `types`：证券类型，默认 `stock`
- `date`：查询日期；回测中默认取当前回测日

返回：

- DataFrame

### `get_index_stocks(index_symbol, date=None)`

用途：获取指数成分股列表。

参数：

- `index_symbol`：指数代码，如 `000300.XSHG`
- `date`：查询日期；回测中默认取当前回测日

返回：

- 成分股代码列表

### `get_split_dividend(security, start_date=None, end_date=None)`

用途：获取分红、送股、拆分事件。

参数：

- `security`：单个证券代码
- `start_date`：起始日期
- `end_date`：结束日期

返回：

- 事件列表
- 统一字段包括：`security`、`date`、`security_type`、`scale_factor`、`bonus_pre_tax`、`per_base`

## 6. 订单与组合

### `order(security, amount, price=None, style=None, wait_timeout=None)`

用途：按股数下单。

参数：

- `security`：证券代码
- `amount`
  - `> 0`：买入股数
  - `< 0`：卖出股数
- `price`
  - 传值时表示显式价格
  - 常用于限价单快捷写法
- `style`
  - `None`：默认下单样式
  - `LimitOrderStyle(price)`：限价单
  - `MarketOrderStyle(...)`：市价保护单
- `wait_timeout`
  - 仅 live 有效
  - `None`：走全局 `TRADE_MAX_WAIT_TIME`
  - `> 0`：同步等待指定秒数
  - `0`：异步立即返回

### `order_value(security, value, price=None, style=None, wait_timeout=None)`

用途：按金额下单。

参数：

- `security`：证券代码
- `value`：目标下单金额
- 其余参数与 `order` 一致

注意：

- 实际股数由撮合价格换算

### `order_target(security, amount, price=None, style=None, wait_timeout=None)`

用途：把持仓调整到目标股数。

参数：

- `security`：证券代码
- `amount`：目标持仓股数
- 其余参数与 `order` 一致

### `order_target_value(security, value, price=None, style=None, wait_timeout=None)`

用途：把持仓调整到目标市值。

参数：

- `security`：证券代码
- `value`：目标持仓市值
- 其余参数与 `order` 一致

### `cancel_order(order_or_id)`

- `order_or_id`：订单对象或订单 ID
- 用途：撤销单个订单

### `cancel_all_orders()`

- 用途：撤销本地队列里的所有订单

### `get_open_orders()`

- 用途：获取当日未完成订单
- 返回：`{order_id: Order}`

### `get_orders(order_id=None, security=None, status=None, from_broker=False)`

参数：

- `order_id`：可选，过滤特定订单
- `security`：可选，过滤特定证券
- `status`：可选，订单状态或状态字符串
- `from_broker`
  - `False`：返回引擎视角订单
  - `True`：返回券商侧全量订单

返回：

- `dict`

### `get_trades(order_id=None, security=None)`

参数：

- `order_id`：可选，过滤特定订单对应成交
- `security`：可选，过滤特定证券

返回：

- `dict`
- `key` 通常是 `trade_id`

### `MarketOrderStyle(limit_price=None, buy_price_percent=None, sell_price_percent=None)`

参数：

- `limit_price`：保护价
- `buy_price_percent`：买入时相对参考价的偏移比例
- `sell_price_percent`：卖出时相对参考价的偏移比例

用途：

- live 中发送带保护价的市价单

### `LimitOrderStyle(price)`

- `price`：限价价格
- 用途：显式限价下单

## 7. Tick 订阅

### `subscribe(security_or_list, frequency='tick')`

参数：

- `security_or_list`
  - 单个代码
  - 代码列表
  - 本地 xtdata 场景下也可传 `['SH', 'SZ']` 做全市场订阅
- `frequency`
  - 当前仅支持 `'tick'`

用途：

- 注册 tick 订阅

### `unsubscribe(security_or_list, frequency='tick')`

- 参数与 `subscribe` 类似
- 用途：取消部分订阅

### `unsubscribe_all()`

- 用途：取消全部 tick 订阅

### `get_current_tick(security)`

- `security`：证券代码
- 返回：最简快照，如 `{'sid': code, 'last_price': price, 'dt': ts}`

### `handle_tick(context, tick)`

- `context`：运行上下文
- `tick`：tick 字典
- 用途：定义后会被框架自动回调

注意：

- 远程 `qmt-remote` 主要是轮询快照，不是低延迟推送
- live 重启后建议在 `process_initialize(context)` 中重新 `subscribe`

## 8. 研究环境 I/O

### `read_file(path)`

- `path`：研究根目录下的相对路径
- 返回：文件原始内容

### `write_file(path, content, append=False)`

参数：

- `path`：研究根目录下的相对路径
- `content`
  - 支持 `str`
  - 支持 `bytes`、`bytearray`、`memoryview`
- `append`
  - `False`：覆盖写
  - `True`：追加写

注意：

- 不允许越界到研究根目录之外
- 未初始化研究目录时会提示先运行 `bullet-trade lab`

## 9. 常用工具

### `print_portfolio_info(context, top_n=None)`

- `context`：运行上下文
- `top_n`：可选，打印前 N 大持仓
- 用途：快速查看资产、现金、持仓摘要

### `prettytable_print_df(df, headers='keys', show_index=False, max_rows=50)`

- `df`：DataFrame
- `headers`：表头样式，默认 `keys`
- `show_index`：是否显示索引
- `max_rows`：最多显示多少行
- 用途：在日志中整齐打印 DataFrame

## 10. 常用数据模型

### `Context`

高频属性：

- `portfolio`
- `current_dt`
- `previous_dt`
- `previous_date`
- `run_params`
- `subportfolios`

### `Portfolio`

高频属性：

- `total_value`
- `available_cash`
- `locked_cash`
- `positions`

### `Position`

高频属性：

- `total_amount`
- `closeable_amount`
- `avg_cost`
- `price`
- `value`
- `side`

### `Order`

高频属性通常包括：

- `order_id`
- `security`
- `amount`
- `filled`
- `status`

### `Trade`

高频属性通常包括：

- `trade_id`
- `security`
- `amount`
- `price`

## 11. 不要默认假设可用的能力

以下 JoinQuant 能力不要直接写进通用策略：

- `get_fundamentals`
- `query`
- 其他基础面 / 财务 API

如果需求必须依赖这些接口，先读 `data-providers.md`，再明确策略是否只支持 `jqdata`。
