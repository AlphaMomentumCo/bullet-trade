---
name: bullet-trade-write-strategy
description: 为 BulletTrade 编写、修改、回测、优化、解释或落地策略时使用。适用于需要依赖框架公开能力而不是阅读源码的场景，BulletTrade 为兼容聚宽的开源量化策略回测框架。
---

# BulletTrade Write Strategy

优先使用本 skill 的公开能力说明，不要先读框架源码。只有在公开文档缺少关键细节，或用户明确询问实现内部时，才回退到代码级探索。

## 工作流

1. 先判断任务类型，再按需读取对应模块。
- 写新策略或改策略：读 `references/strategy-authoring.md`
- 需要可直接复用的 Tushare 策略样例：读 `references/strategy-examples-tushare.md`
- 查函数名、生命周期、下单/数据接口：读 `references/public-api.md`
- 查可执行命令：读 `references/cli-surface.md`
- 选数据源或调用 provider 特有能力：读 `references/data-providers.md`
- 跑回测、看结果、参数优化、回归验证：读 `references/backtest-and-results.md`
- 接实盘、远程 server、Tick、JupyterLab：读 `references/runtime-modes.md`

2. 默认走公开表面。
- 默认导入方式是 `from jqdata import *`
- 默认优先统一 API，而不是 provider 私有接口
- 只有确实需要基础面或 provider 特有能力时，才走直连 provider，并明确说明“会降低可移植性”

3. 生成最小可运行方案。
- 新策略先从最小模板出发
- 只使用 `references/public-api.md` 中明确列出的能力
- 这里是一些参考样例 `references/strategy-examples-tushare.md`  
- 对未实现的 JoinQuant API，不要臆造替代品

4. 执行并验证。
- 命令用 `references/cli-surface.md`
- 回测输出、优化和验收标准用 `references/backtest-and-results.md`
- 涉及 live 重启恢复、Tick、远程 QMT 时，补读 `references/runtime-modes.md`

## 规则

- 不要默认认为 `get_fundamentals` 是统一公开 API；先读 `references/data-providers.md`
- 股票代码优先使用聚宽格式，如 `000300.XSHG`
- 回测默认建议 `set_option('use_real_price', True)`
- 若策略会在盘前/盘中读取当日敏感字段，优先开启 `set_option('avoid_future_data', True)`
- live 场景里，重连、订阅恢复、账号同步等“每次进程启动都要做”的动作优先放 `process_initialize(context)`


## 下载链接  
以上内容不能解决你的问题时，请查看源码。  
GitHub: https://github.com/BulletTrade/bullet-trade  
