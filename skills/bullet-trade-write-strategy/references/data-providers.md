# BulletTrade Data Providers

## 推荐选择

- 纯回测 / 策略开发：优先 `jqdata`
- 本地 QMT 实盘：`qmt`
- 远程 Windows QMT 服务端 + 跨平台客户端：`qmt-remote`
- 无聚宽账号但有 TuShare：`tushare`

## 最小配置

### `jqdata`

```env
DEFAULT_DATA_PROVIDER=jqdata
JQDATA_USERNAME=your_username
JQDATA_PASSWORD=your_password
```

### `qmt`

```env
DEFAULT_DATA_PROVIDER=qmt
QMT_DATA_PATH=C:\国金QMT交易端\userdata_mini
```

### `qmt-remote`

```env
DEFAULT_DATA_PROVIDER=qmt-remote
QMT_SERVER_HOST=10.0.0.8
QMT_SERVER_PORT=58620
QMT_SERVER_TOKEN=secret
```

### `tushare`

```env
DEFAULT_DATA_PROVIDER=tushare
TUSHARE_TOKEN=your_token
```

## 关键支持矩阵

| API | jqdata | qmt/miniqmt | qmt-remote | tushare |
| --- | --- | --- | --- | --- |
| `get_price` | 支持 | 支持 | 支持 | 支持 |
| `attribute_history` | 支持 | 支持 | 支持 | 支持 |
| `get_current_data` | 支持 | 支持 | 支持 | 支持 |
| `get_current_tick` | 支持 | 支持 | 支持 | 不支持 |
| `get_all_securities` | 支持 | 支持 | 支持 | 支持 |
| `get_index_stocks` | 支持 | 支持 | 支持 | 支持 |
| `get_split_dividend` | 支持 | 支持 | 支持 | 支持 |
| 基础面 / 财务 | 强 | 弱 | 弱 | 部分 |

## 代码格式建议

- 统一优先使用聚宽格式：
  - 上海：`601318.XSHG`
  - 深圳：`000001.XSHE`
- `qmt` 通常也能接受并自动转换
- 若直接写 `601318.SH` / `000001.SZ`，会降低跨 provider 一致性

## 运行时切换

```python
from bullet_trade.data.api import set_data_provider

set_data_provider('jqdata')
set_data_provider('qmt')
set_data_provider('tushare')
```

## provider 直连

当统一 API 不够时，可以按名称拿实例：

```python
from bullet_trade.data.api import get_data_provider

jq = get_data_provider("jqdata")
```

规则：

- 不会修改默认 provider
- 会缓存实例
- 会在获取时认证
- 只会回退到“同一 provider 的 SDK/客户端”，不会跨 provider

## 关于基础面策略

如果用户要写小市值、低估值、财务筛选等策略，先做这个判断：

1. 是否接受仅支持 `jqdata`
2. 是否愿意使用 provider 直连

若两个问题都接受，才能继续写基础面策略。否则应改成价格量能类策略，或明确提示能力边界。
