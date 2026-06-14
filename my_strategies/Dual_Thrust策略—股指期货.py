'''
1、首先计算:
(1)N日High的最高价HH, N日Close的最低价LC;
(2)N日Close的最高价HC，N日Low的最低价LL;
(3)Range = Max(HH-LC,HC-LL)
(4)BuyLine = Open + K1*Range
(5)SellLine = Open + K2*Range

2.构造系统
(1)当价格向上突破上轨时，如果当时持有空仓，则先平仓，再开多仓；如果没有持仓，则直接开多仓；
(2)当价格向下突破下轨时，如果当时持有多仓，则先平仓，再开空仓；如果没有持仓，则直接开空仓；
'''

from jqdata import *
from bullet_trade.data.providers import jqdata as _jqdata_provider

query = _jqdata_provider.query
valuation = _jqdata_provider.jq.valuation
balance = _jqdata_provider.jq.balance

def initialize(context):
    # 设定沪深300作为基准
    set_benchmark('000300.XSHG')
    # True为开启动态复权模式，使用真实价格交易
    set_option('use_real_price', True) 
    # 设定成交量比例
    set_option('order_volume_ratio', 1)
    # 关闭订单提醒
    # log.set_level('order', 'error')
    # 设定期货保证金比例
    set_option('futures_margin_rate', 0.3)
    # 金融期货close_today_commission可不用设定，平今仓默认0.0023
    set_order_cost(OrderCost(open_commission=0.000023, close_commission=0.000023, close_today_commission=0.0023), type='index_futures')
    #运行函数
    run_daily(set_info, time='before_open')
    run_daily(trade, time='every_bar')

def set_info(context):
    # 分钟计数
    g.minute_count = 0

def trade(context):
    # 开盘第一分钟
    if g.minute_count == 0:
        # 获取当月可交易的 HS300 股指期货合约
        g.security = get_stock_index_futrue_code(context,symbol='IF',month='current_month')
        # 获取 BuyLine, SellLine
        g.BuyLine, g.SellLine = dual_thrust(g.security,n=10,K1=0.5,K2=0.5)
        # 分钟计数
        g.minute_count += 1
    # 开盘第一分钟之后
    else:
        # 当前版本仅维护 positions，不区分 long_positions/short_positions
        pos = context.portfolio.positions.get(g.security)
        long_closeable_amount = pos.closeable_amount if pos else 0
        # 获取标的的最新价
        current_price = attribute_history(g.security, 1, '1m', ['close'], df=False)['close'][0]

        # 当价格向上突破上轨时
        if current_price > g.BuyLine:
            # 当前回测引擎不支持期货双向持仓，这里采用单向逻辑：无仓开多
            if long_closeable_amount == 0:
                order(g.security, 1)
                log.info('没有持仓，开多仓')
        # 当价格向下突破下轨时
        elif current_price < g.SellLine:
            # 破下轨仅平多，不开空（当前引擎不支持期开空逻辑）
            if long_closeable_amount > 0:
                order_target(g.security, 0)
                log.info('持有多仓，破下轨后平仓')

        # 分钟计数
        g.minute_count += 1

## 获取 BuyLine 和 SellLine
def dual_thrust(security,n,K1,K2):
    hist = attribute_history(security, n, '1d', ['high','low','close','open'], df=False)
    HH = max(hist['high'])
    LC = min(hist['close'])
    HC = max(hist['close'])
    LL = min(hist['low'])
    # get_current_data 不提供 day_open，改用最近分钟 open 作为当日开盘参考
    Open = attribute_history(security, 1, '1m', ['open'], df=False)['open'][0]
    # 获取 Range
    Range = max((HH-LC),(HC-LL))
    # 计算BuyLine 和 SellLine
    
    BuyLine = Open + K1 * Range
    SellLine = Open - K2 * Range
    # 返回结果
    return BuyLine, SellLine

## 获取当天时间正在交易的股指期货合约
def get_stock_index_futrue_code(context,symbol,month='current_month'):
    '''
    获取当天时间正在交易的股指期货合约。其中:
    symbol:
            'IF' #沪深300指数期货
            'IC' #中证500股指期货
            'IH' #上证50股指期货
    month:
            'current_month' #当月
            'next_month'    #隔月
            'next_quarter'  #下季
            'skip_quarter'  #隔季
    '''
    display_name_dict = {'IF':'沪深300指数期货','IC':'中证500股指期货','IH':'上证50股指期货'}
    month_dict = {'current_month':0, 'next_month':1, 'next_quarter':2, 'skip_quarter':3}

    display_name = display_name_dict[symbol]
    n = month_dict[month]
    dt = context.current_dt.date()
    a = get_all_securities(types=['futures'], date=dt)
    try:
        df = a[(a.display_name == display_name) & (a.start_date <= dt) & (a.end_date >= dt)]
        if (len(df)>4) and (month in ('next_quarter','skip_quarter')):
            return df.index[n+1]
        else:
            return df.index[n]
    except:
        return 'WARRING: 无此合约'

# 获取金融期货合约到期日
def get_CCFX_end_date(fature_code):
    return get_security_info(fature_code).end_date
