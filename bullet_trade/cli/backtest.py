"""
回测命令处理

运行策略回测
"""

from pathlib import Path
from typing import Optional, Union


def _resolve_auto_report_output(output_dir: Path, args) -> Optional[Union[Path, str]]:
    """
    为自动报告选择默认输出路径，避免覆盖详细版 report.html。

    当用户未显式指定 --report-output 时，标准化报告默认写入
    <output>/standard_report.<fmt>。
    """
    explicit_output = getattr(args, "report_output", None)
    if explicit_output:
        return explicit_output
    report_format = getattr(args, "report_format", "html")
    return output_dir / f"standard_report.{report_format}"


def run_backtest(args):
    """
    运行回测

    Args:
        args: 命令行参数

    Returns:
        退出码
    """
    print("=" * 60)
    print("BulletTrade - 策略回测")
    print("=" * 60)
    print()

    # 验证策略文件
    strategy_file = Path(args.strategy_file)
    if not strategy_file.exists():
        print(f"❌ 策略文件不存在: {strategy_file}")
        return 1

    output_dir = Path(args.output).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    log_file = None
    if getattr(args, "generate_logs", True):
        log_file = args.log or str(output_dir / "backtest.log")

    print(f"策略文件: {strategy_file}")
    print(f"回测区间: {args.start} 至 {args.end}")
    print(f"初始资金: {args.cash:,.0f}")
    frequency = getattr(args, "frequency", "day")
    print(f"回测频率: {frequency}")
    if args.benchmark:
        print(f"基准指数: {args.benchmark}")
    print(f"输出目录: {output_dir}")
    print(f"日志文件: {log_file if log_file else '仅终端输出'}")
    print(f"生成图片: {'是' if getattr(args, 'generate_images', False) else '否'}")
    print(f"导出CSV: {'是' if getattr(args, 'generate_csv', True) else '否'}")
    print(f"生成HTML: {'是' if getattr(args, 'generate_html', True) else '否'}")
    if getattr(args, "backtest_data_session", False):
        print("回测数据会话优化: 启用")
    else:
        print("回测数据会话优化: 默认/环境变量")
    if getattr(args, "backtest_data_session", False):
        print("内存行情块缓存: 启用（随数据会话默认开启）")
    else:
        print(
            f"内存行情块缓存: {'启用' if getattr(args, 'backtest_price_block_cache', False) else '关闭'}"
        )
    if getattr(args, "auto_report", False):
        print(f"自动报告: 是 ({getattr(args, 'report_format', 'html').upper()})")
    else:
        print("自动报告: 否")
    print()

    try:
        # 导入回测引擎
        from bullet_trade.core.analysis import generate_report
        from bullet_trade.core.engine import create_backtest

        # 运行回测
        print("开始回测...")
        data_session_config = None
        if getattr(args, "backtest_data_session", False):
            manifest_path = getattr(args, "backtest_data_session_manifest", None)
            if not manifest_path:
                manifest_path = str(output_dir / "backtest_data_session_manifest.json")
            data_session_config = {
                "enabled": True,
                "manifest_path": manifest_path,
                # 开启数据会话时默认启用行情块缓存（可用 --backtest-price-block-cache 显式打开）
                "price_block_cache_enabled": True,
            }
            max_bytes = getattr(args, "backtest_data_session_max_bytes", None)
            if max_bytes is not None:
                data_session_config["max_cache_bytes"] = max_bytes
        results = create_backtest(
            strategy_file=str(strategy_file),
            start_date=args.start,
            end_date=args.end,
            frequency=getattr(args, "frequency", "day"),
            initial_cash=args.cash,
            benchmark=args.benchmark,
            log_file=log_file,
            data_session_config=data_session_config,
        )

        # 生成报告
        print(f"\n生成报告到: {output_dir}")
        generate_report(
            results,
            output_dir=str(output_dir),
            gen_images=getattr(args, "generate_images", False),
            gen_csv=getattr(args, "generate_csv", True),
            gen_html=getattr(args, "generate_html", True),
        )

        if getattr(args, "auto_report", False):
            try:
                from bullet_trade.reporting import ReportGenerationError, generate_cli_report

                metrics_keys = None
                if getattr(args, "report_metrics", None):
                    metrics_keys = [
                        item.strip() for item in str(args.report_metrics).split(",") if item.strip()
                    ] or None
                report_output = _resolve_auto_report_output(output_dir, args)
                report_path = generate_cli_report(
                    input_dir=str(output_dir),
                    output_path=str(report_output) if report_output is not None else None,
                    fmt=getattr(args, "report_format", "html"),
                    template_path=getattr(args, "report_template", None),
                    metrics_keys=metrics_keys,
                    title=getattr(args, "report_title", None),
                )
                print(f"\n自动报告生成完成: {report_path}")
            except ReportGenerationError as exc:
                print(f"\nWarning: 自动报告生成失败: {exc}")
            except Exception as exc:  # pragma: no cover
                print(f"\nWarning: 自动报告过程出现未预期的错误: {exc}")
                import traceback

                traceback.print_exc()

        # 打印简要结果
        metrics = results.get("metrics", {})

        def _percent(key: str) -> float:
            val = metrics.get(key)
            if isinstance(val, (int, float)):
                return val / 100.0
            return 0.0

        def _scalar(key: str) -> float:
            val = metrics.get(key)
            if isinstance(val, (int, float)):
                return float(val)
            return 0.0

        if not metrics and "daily_records" in results:
            try:
                from bullet_trade.core.analysis import calculate_metrics

                metrics = calculate_metrics(results)
                results["metrics"] = metrics
            except Exception:
                metrics = {}

        print("\n" + "=" * 60)
        print("回测结果")
        print("=" * 60)
        print(f"总收益率: {_percent('策略收益'):.2%}")
        print(f"年化收益率: {_percent('策略年化收益'):.2%}")
        print(f"最大回撤: {_percent('最大回撤'):.2%}")
        print(f"夏普比率: {_scalar('夏普比率'):.2f}")
        print("=" * 60)
        print(f"\n✓ 回测完成！详细报告已保存至 {output_dir}")

        return 0

    except ImportError as e:
        print(f"❌ 导入错误: {e}")
        print("\n提示: 请确保已正确安装 BulletTrade")
        print("  pip install -e .")
        return 1

    except Exception as e:
        print(f"❌ 回测失败: {e}")
        import traceback

        traceback.print_exc()
        return 1
