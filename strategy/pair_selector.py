import ccxt
import time
from typing import Dict, List, Any


def init_mainnet_exchange() -> ccxt.deribit:
    """Initializes a read-only exchange connection to Deribit Mainnet for accurate market data."""
    exchange = ccxt.deribit({
        'enableRateLimit': True,
        'timeout': 15000,
    })
    return exchange


def init_testnet_exchange() -> ccxt.deribit:
    """Initializes Deribit Sandbox Testnet connection for trading/order operations."""
    exchange = ccxt.deribit({
        'enableRateLimit': True,
        'timeout': 15000,
    })
    exchange.set_sandbox_mode(True)
    return exchange


def calculate_spread_friction(spot_ticker: Dict, perp_ticker: Dict) -> float:
    """Calculates entry cross-spread friction percentage."""
    spot_bid = spot_ticker.get('bid') or spot_ticker.get('last') or 0.0
    perp_ask = perp_ticker.get('ask') or perp_ticker.get('last') or 0.0

    if spot_bid <= 0 or perp_ask <= 0:
        return 999.0

    friction_pct = ((perp_ask - spot_bid) / spot_bid) * 100.0
    return round(friction_pct, 4)


def fetch_market_snapshot(target_bases: List[str] = None) -> List[Dict[str, Any]]:
    """
    Fetches spot/perp tickers, min order amounts, and funding rates from MAINNET.
    This provides real-time liquidity spreads and accurate live funding yield metrics.
    """
    if target_bases is None:
        target_bases = ['BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'LINK', 'PAXG', 'STETH']

    # Read market data from Live Mainnet
    exchange = init_mainnet_exchange()
    snapshot: List[Dict[str, Any]] = []

    try:
        markets = exchange.load_markets()
        print(f"[✓] Connected to Deribit Mainnet. Loaded {len(markets)} markets.")
        print(f"[*] Scanning live mainnet tickers & funding rates for {len(target_bases)} pairs...\n")
    except Exception as e:
        print(f"[!] Failed to connect to Deribit Mainnet: {e}")
        return []

    for base in target_bases:
        spot_symbol = f"{base}/USDC"
        perp_symbol = f"{base}/USDC:USDC"

        if spot_symbol not in markets or perp_symbol not in markets:
            continue

        try:
            spot_market = markets[spot_symbol]
            spot_ticker = exchange.fetch_ticker(spot_symbol)
            perp_ticker = exchange.fetch_ticker(perp_symbol)

            min_spot_amount = spot_market.get('limits', {}).get('amount', {}).get('min') or 0.01
            spot_price = spot_ticker.get('last') or spot_ticker.get('ask') or 1.0
            min_trade_cost = round(min_spot_amount * spot_price, 2)

            spread_friction = calculate_spread_friction(spot_ticker, perp_ticker)

            funding_rate = 0.0
            try:
                funding_info = exchange.fetch_funding_rate(perp_symbol)
                funding_rate = float(funding_info.get('fundingRate') or 0.0) * 100.0
            except Exception:
                pass

            snapshot.append({
                'base': base,
                'spot_symbol': spot_symbol,
                'perp_symbol': perp_symbol,
                'min_cost': min_trade_cost,
                'friction': spread_friction,
                'funding_rate': funding_rate,
            })

            time.sleep(0.1)  # Respect public rate limits

        except Exception:
            continue

    return snapshot


def evaluate_pairs(
    market_data: List[Dict[str, Any]],
    account_balance: float = 100.0,
    max_allocation_pct: float = 0.20,
    max_friction_pct: float = 0.25,
    min_funding_pct: float = 0.0
):
    """
    Evaluates pre-fetched mainnet snapshot against capital profile parameters instantly.
    """
    max_capital_per_trade = account_balance * max_allocation_pct
    results: List[Dict[str, Any]] = []

    for item in market_data:
        is_viable = True
        reason = "Fits Budget"

        if item['min_cost'] > max_capital_per_trade:
            is_viable = False
            reason = "Too Costly"
        elif item['friction'] > max_friction_pct or item['friction'] < -1.0:
            is_viable = False
            reason = "Wide Spread"
        elif item['funding_rate'] <= min_funding_pct:
            is_viable = False
            reason = "Low Yield"

        results.append({
            **item,
            'viable': is_viable,
            'reason': reason
        })

    # Render Table Grid
    print("=" * 88)
    print(f"{'ASSET':<6} | {'SPOT':<10} | {'PERP':<15} | {'COST ($)':<8} | {'FRICTION':<9} | {'FUNDING':<8} | {'VIABLE':<6} | {'REASON':<10}")
    print("-" * 88)

    for r in results:
        viable_str = "YES" if r['viable'] else "NO"
        print(
            f"{r['base']:<6} | "
            f"{r['spot_symbol']:<10} | "
            f"{r['perp_symbol']:<15} | "
            f"{r['min_cost']:>8.2f} | "
            f"{r['friction']:>8.2f}% | "
            f"{r['funding_rate']:>7.4f}% | "
            f"{viable_str:^6} | "
            f"{r['reason']:<10}"
        )

    print("=" * 88)

    # Top Selection
    viable_pairs = [p for p in results if p['viable']]

    if viable_pairs:
        best_pair = sorted(viable_pairs, key=lambda x: x['funding_rate'], reverse=True)[0]
        print(f"[★] TOP RECOMMENDED PAIR: {best_pair['base']}")
        print(f"    ├── Spot: {best_pair['spot_symbol']} | Perp: {best_pair['perp_symbol']}")
        print(f"    ├── Min Capital Req: ${best_pair['min_cost']:.2f} USDC")
        print(f"    ├── Spread Friction: {best_pair['friction']:.2f}%")
        print(f"    └── Estimated 8h Funding Rate: {best_pair['funding_rate']:.4f}%\n")
    else:
        budget_pairs = [p for p in results if p['min_cost'] <= max_capital_per_trade]
        if budget_pairs:
            best_fallback = sorted(budget_pairs, key=lambda x: x['funding_rate'], reverse=True)[0]
            print(f"[!] NO FULLY VIABLE PAIRS FOUND.")
            print(f"[★] BEST AVAILABLE FALLBACK: {best_fallback['base']}")
            print(f"    ├── Spot: {best_fallback['spot_symbol']} | Perp: {best_fallback['perp_symbol']}")
            print(f"    ├── Min Capital Req: ${best_fallback['min_cost']:.2f} USDC")
            print(f"    └── Estimated 8h Funding Rate: {best_fallback['funding_rate']:.4f}%\n")