# Runtime Modes

## 1. 本地 QMT live

最小 `.env`：

```env
DEFAULT_DATA_PROVIDER=qmt
DEFAULT_BROKER=qmt
QMT_DATA_PATH=C:\国金QMT交易端\userdata_mini
QMT_ACCOUNT_ID=123456
```

运行：

```bash
bullet-trade live strategy.py --broker qmt
```

## 2. 远程 qmt-remote live

客户端最小 `.env`：

```env
DEFAULT_DATA_PROVIDER=qmt-remote
DEFAULT_BROKER=qmt-remote
QMT_SERVER_HOST=10.0.0.8
QMT_SERVER_PORT=58620
QMT_SERVER_TOKEN=secret
```

运行：

```bash
bullet-trade live strategy.py --broker qmt-remote
```

## 3. 远程 server

服务端最小 `.env`：

```env
QMT_DATA_PATH=C:\国金QMT交易端\userdata_mini
QMT_ACCOUNT_ID=123456
QMT_SERVER_TOKEN=secret
```

启动：

```bash
bullet-trade --env-file .env server --listen 0.0.0.0 --port 58620 --enable-data --enable-broker
```

多账户时再补：

- `--accounts main=123456`
- `QMT_SERVER_ACCOUNT_KEY=main`

## 4. 聚宽模拟盘远程下单 helper

上传：

- `helpers/bullet_trade_jq_remote_helper.py`

最小用法：

```python
import bullet_trade_jq_remote_helper as bt

bt.configure(host="your.server.ip", port=58620, token="secret")
```

## 5. Tick

策略侧常用接口：

- `subscribe([...], 'tick')`
- `unsubscribe([...], 'tick')`
- `unsubscribe_all()`
- `get_current_tick('000001.XSHE')`
- `handle_tick(context, tick)`

结论：

- 本地 xtdata 更接近推送
- 远程 qmt-remote 主要是轮询快照
- live 重启后建议在 `process_initialize` 里重新订阅

## 6. 研究环境

启动：

```bash
bullet-trade lab
bullet-trade lab --diagnose
```

用途：

- 跑 Notebook
- 使用 `read_file` / `write_file`
- 直接在研究根目录下复用 `.env`、策略、回测输出
