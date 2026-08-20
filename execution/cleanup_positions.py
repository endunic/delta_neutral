import sys
import os
import time
import json
import re

# Resolve project root and add to sys.path
package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if package_root not in sys.path:
    sys.path.insert(0, package_root)
import ccxt
import config.settings as settings


def close_all_positions():
    print("=" * 60)
    print("      DERIBIT TESTNET: POSITION & BALANCE CLEANUP       ")
    print("=" * 60)

    exchange = ccxt.deribit({
        'apiKey': getattr(settings, 'DERIBIT_API_KEY', ''),
        'secret': getattr(settings, 'DERIBIT_API_SECRET', ''),
        'enableRateLimit': True,
    })
    exchange.set_sandbox_mode(True)

    try:
        exchange.load_markets()
        print("[✓] Connected to Deribit Testnet.")
    except Exception as e:
        print(f"[!] Connection failed: {e}")
        return

    spot_symbol = "BNB/USDC"
    perp_symbol = "BNB/USDC:USDC"

    # 1. CHECK & CLOSE PERP POSITIONS
    print("\n[*] Checking Perpetual Positions...")
    try:
        positions = exchange.fetch_positions(symbols=[perp_symbol])
        open_perp_qty = 0.0

        for pos in positions:
            contracts = float(pos.get('contracts', 0.0) or 0.0)
            if abs(contracts) > 0:
                side = pos.get('side', '')
                open_perp_qty = contracts
                print(f"[!] Active Perp position found: {side.upper()} {contracts} {perp_symbol}")

                close_side = 'buy' if side.lower() == 'short' else 'sell'
                order = exchange.create_order(
                    symbol=perp_symbol,
                    type='market',
                    side=close_side,
                    amount=contracts,
                    params={'reduceOnly': True}
                )
                print(f"[✓] Closed Perp position | Order ID: {order.get('id')}")

        if open_perp_qty == 0.0:
            print("[✓] No open Perpetual positions found.")

    except Exception as e:
        print(f"[!] Error fetching/closing perp positions: {e}")

    # 2. DYNAMICALLY LIQUIDATE SPOT ASSET BALANCES
    print("\n[*] Checking Spot Asset Balances...")

    # Initial optimistic target chunk size
    # The Deribit testnet "max_spot_order_quantity" error actually reports the
    # exchange-side minimum/maximum order size in its payload (data.limit). We
    # start conservative and adapt to the value returned by the API.
    current_chunk_size = 10.0
    min_chunk_size = 0.0001
    # Hard safety floor: if a single chunk cannot exceed this, the loop below
    # must still make forward progress or bail out to avoid an infinite loop.
    absolute_floor = 0.0001
    sold_total = 0.0
    chunk_count = 1
    consecutive_limit_failures = 0
    max_consecutive_failures = 5

    while True:
        try:
            balance = exchange.fetch_balance()
            free_bnb = float(balance.get('BNB', {}).get('free', 0.0) or 0.0)

            if free_bnb < absolute_floor:
                print(f"\n[✓] Spot balance fully cleared! Final Free Balance: {free_bnb:.4f} BNB")
                break

            # Respect the exchange limit as a hard ceiling for a single chunk.
            chunk_qty = min(current_chunk_size, free_bnb)
            chunk_qty = float(exchange.amount_to_precision(spot_symbol, chunk_qty))

            if chunk_qty < absolute_floor:
                print(f"\n[!] Computed chunk size {chunk_qty} below minimum tradeable floor. "
                      f"Remaining free balance: {free_bnb:.4f} BNB cannot be sold via market order.")
                break

            print(f" [*] Attempting Chunk {chunk_count}: Selling {chunk_qty} BNB (Chunk size cap: {current_chunk_size} BNB | Free: {free_bnb:.2f} BNB)...")
            order = exchange.create_market_sell_order(spot_symbol, chunk_qty)

            sold_total += chunk_qty
            print(f" [✓] Chunk {chunk_count} Success | Sold: {chunk_qty} BNB | ID: {order.get('id')}")
            chunk_count += 1
            consecutive_limit_failures = 0

            # On successful execution, slightly scale up chunk size for faster liquidation
            current_chunk_size = min(current_chunk_size * 1.2, 50.0)
            time.sleep(0.2)

        except Exception as err:
            err_str = str(err)

            # If rejected due to max/min order quantity limits, parse the real
            # limit from the API payload and adapt to it instead of guessing.
            if "max_spot_order_quantity" in err_str or "11022" in err_str or "Invalid params" in err_str:
                api_limit = _parse_exchange_limit(err_str)
                if api_limit is not None and api_limit > 0:
                    print(f" [!] Exchange reports order limit {api_limit} BNB. "
                          f"Adapting chunk size: {current_chunk_size} -> {api_limit} BNB")
                    current_chunk_size = api_limit
                else:
                    # No parseable limit: fall back to halving, but keep a floor.
                    new_chunk_size = max(current_chunk_size / 2.0, absolute_floor)
                    print(f" [!] Exceeded exchange limit. Adapting chunk size: {current_chunk_size} -> {new_chunk_size:.5f} BNB")
                    current_chunk_size = new_chunk_size

                consecutive_limit_failures += 1
                if consecutive_limit_failures >= max_consecutive_failures:
                    print(f" [!] {consecutive_limit_failures} consecutive limit failures at "
                          f"chunk size {current_chunk_size} BNB. Aborting spot liquidation to "
                          f"avoid an infinite loop. Remaining free balance must be cleared manually.")
                    break
            else:
                consecutive_limit_failures = 0
                print(f" [!] Transient error on chunk {chunk_count}: {err}. Retrying in 1s...")

            time.sleep(0.5)

    print(f"\n[✓] Total Liquidated Spot Balance: {sold_total:.4f} BNB")


def _parse_exchange_limit(err_str):
    """Extract the 'limit' value from a Deribit JSON-RPC error payload.

    Example payload:
        {"jsonrpc":"2.0","error":{"code":11022,"data":{"limit":"200.0","currency":"BNB"},
         "message":"max_spot_order_quantity"}, ...}
    Returns the limit as a float, or None if it cannot be parsed.
    """
    try:
        # Strip any leading traceback text to find the JSON object.
        match = re.search(r'\{.*\}', err_str, re.DOTALL)
        if not match:
            return None
        payload = json.loads(match.group(0))
        data = payload.get('error', {}).get('data', {})
        limit = data.get('limit')
        if limit is None:
            return None
        return float(limit)
    except (ValueError, AttributeError, TypeError):
        return None

    print("=" * 60)
    print("[✓] Cleanup procedure complete. Exposure zeroed out.")
    print("=" * 60)


if __name__ == "__main__":
    close_all_positions()