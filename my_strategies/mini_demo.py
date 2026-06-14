'''
小市值调仓 demo：真实回测成交后通过 send_msg 推送飞书交易机器人。
'''

from jqdata import *
from bullet_trade.data.providers import jqdata as _jqdata_provider

query = _jqdata_provider.query
valuation = _jqdata_provider.jq.valuation


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('order_volume_ratio', 1)
    set_order_cost(
        OrderCost(
            open_tax=0,
            close_tax=0.001,
            open_commission=0.0003,
            close_commission=0.0003,
            close_today_commission=0,
            min_commission=5,
        ),
        type='stock',
    )
    g.stocknum = 3
    g.days = 0
    g.refresh_rate = 5
    g.notified_trade_ids = set()

    run_daily(trade, 'open')
    # 开盘撮合完成后推送当日新成交（trade 在 9:30 下单，9:35 读取 get_trades）
    run_daily(notify_trade_fills, '09:35')


def check_stocks(context):
    q = query(
        valuation.code,
        valuation.market_cap,
    ).filter(
        valuation.market_cap.between(20, 30),
    ).order_by(
        valuation.market_cap.asc(),
    )
    df = get_fundamentals(q)
    buylist = list(df['code'])
    buylist = filter_paused_stock(buylist)
    return buylist[:g.stocknum]


def trade(context):
    if g.days % g.refresh_rate != 0:
        g.days += 1
        return

    sell_list = list(context.portfolio.positions.keys())
    if sell_list:
        for stock in sell_list:
            order_target_value(stock, 0)

    if len(context.portfolio.positions) < g.stocknum:
        num = g.stocknum - len(context.portfolio.positions)
        cash = context.portfolio.available_cash / num if num > 0 else 0
    else:
        cash = 0

    stock_list = check_stocks(context)
    for stock in stock_list:
        if len(context.portfolio.positions.keys()) < g.stocknum:
            order_value(stock, cash)

    g.days = 1


def filter_paused_stock(stock_list):
    current_data = get_current_data()
    return [stock for stock in stock_list if not current_data[stock].paused]


def _format_trade_message(context, trade):
    """用当日真实成交与行情数据组装通知文本。"""
    security = trade.security
    is_buy = trade.amount > 0
    side_label = '买入' if is_buy else '卖出'
    amount = abs(int(trade.amount))
    price = float(trade.price)
    order_value = round(price * amount, 2)

    info = get_security_info(security) or {}
    name = info.get('display_name') or info.get('name') or ''

    current_data = get_current_data()
    day_change = None
    if security in current_data:
        sec = current_data[security]
        last_price = getattr(sec, 'last_price', None) or price
        pre_close = getattr(sec, 'pre_close', None)
        if pre_close and pre_close > 0 and last_price:
            day_change = (float(last_price) - float(pre_close)) / float(pre_close) * 100

    emoji = '📈' if is_buy else '📉'
    lines = [f'{emoji} **下单通知**', '']
    if name:
        lines.append(f'**{name}** ({security})')
    else:
        lines.append(f'**{security}**')
    lines.append(f'方向：{side_label}')
    lines.append(f'数量：{amount} 股')
    lines.append(f'金额：¥{order_value:,.2f}')
    lines.append(f'成交价：{price:.2f}')
    if day_change is not None:
        lines.append(f'今日涨跌：{day_change:+.2f}%')
    if trade.order_id:
        lines.append(f'订单ID：{trade.order_id}')
    lines.append(f'时间：{context.current_dt.strftime("%Y-%m-%d %H:%M:%S")}')
    return '\n'.join(lines)


def notify_trade_fills(context):
    """推送当日尚未通知过的真实成交记录。"""
    trades = get_trades()
    if not trades:
        return

    for trade_id, trade in trades.items():
        if trade_id in g.notified_trade_ids:
            continue
        g.notified_trade_ids.add(trade_id)
        send_msg(_format_trade_message(context, trade))
