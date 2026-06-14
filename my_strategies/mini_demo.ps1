# 在项目根目录执行回测
Set-Location (Join-Path $PSScriptRoot "..")
bullet-trade backtest my_strategies/mini_demo.py `
  --start 2026-01-01 --end 2026-01-07 --cash 100000 `
  --output my_results/mini_demo
