import config.settings as settings

def scan_funding_opportunities(exchange):
    """Scans for active perpetual pairs and calculates estimated APR based on 8h funding rates."""
    markets = exchange.load_markets()
    
    spot_symbols = {
        m['symbol'] for m in markets.values() 
        if m.get('spot', False) and m.get('active', True)
    }
    
    perp_symbols = [
        symbol for symbol, m in markets.items() 
        if m.get('swap', False) and m.get('active', True)
    ]
    
    perp_opportunities = []
    for symbol in perp_symbols:
        try:
            base_pair = symbol.split(':')[0]
            if base_pair not in spot_symbols:
                continue
                
            funding_info = exchange.fetch_funding_rate(symbol)
            raw_rate = funding_info.get('fundingRate')
            
            if raw_rate is not None:
                rate_8h = raw_rate * 100
                apr = rate_8h * 3 * 365
                perp_opportunities.append({
                    'symbol': symbol,
                    'spot_symbol': base_pair,
                    'rate_8h': rate_8h,
                    'apr': apr
                })
        except Exception:
            continue
                
    return sorted(perp_opportunities, key=lambda x: x['rate_8h'], reverse=True)


def calculate_trade_sizing(exchange, opportunities):
    """Calculates position sizes based on account balance and minimum contract requirements."""
    balance = exchange.fetch_balance()
    free_usdc = float(balance.get('USDC', {}).get('free', 0.0))

    selected_target = None
    target_amount = 0.0
    target_price = 0.0

    for target in opportunities:
        symbol = target['symbol']
        ticker = exchange.fetch_ticker(symbol)
        last_price = ticker['last']
        
        market_info = exchange.market(symbol)
        min_amount = float(market_info.get('limits', {}).get('amount', {}).get('min', 1.0))
        
        affordable_qty = (free_usdc * settings.ACCOUNT_ALLOCATION_PCT) / last_price if last_price > 0 else 0
        
        if affordable_qty >= min_amount:
            selected_target = target
            target_amount = round(affordable_qty, 2)
            target_price = last_price
            break
        elif free_usdc >= (min_amount * last_price):
            selected_target = target
            target_amount = min_amount
            target_price = last_price
            break

    return selected_target, target_amount, target_price, free_usdc
