import time
import math
import ccxt
from core.logger import setup_logger

logger = setup_logger()

# Deribit Exchange Limits (Kept conservatively under the 200 BNB spot cap)
MAX_SPOT_LIMIT = 190.0
MAX_PERP_LIMIT = 1000.0


def _wait_for_order_fill(exchange, order_id, symbol, max_retries=10, delay=1.0):
    """Polls an open order until filled or max retries reached."""
    for _ in range(max_retries):
        try:
            fetched_order = exchange.fetch_order(order_id, symbol)
            status = str(fetched_order.get('status', '')).lower()
            filled = float(fetched_order.get('filled', 0) or 0)
            
            if status == 'closed' or filled > 0:
                return fetched_order
        except Exception as e:
            logger.warning(f"Transient error checking order fill for {order_id}: {e}")
        time.sleep(delay)
        
    try:
        return exchange.fetch_order(order_id, symbol)
    except Exception:
        return {'id': order_id, 'status': 'open', 'filled': 0.0, 'price': 0.0, 'cost': 0.0}


def _format_order_fill_log(label, order, symbol):
    """Formats standardized output for order execution details."""
    order_id = order.get('id', 'N/A')
    status = str(order.get('status', 'N/A')).upper()
    filled_size = float(order.get('filled', 0) or order.get('amount', 0) or 0.0)
    fill_price = float(order.get('average', 0) or order.get('price', 0) or 0.0)
    total_cost = float(order.get('cost', 0) or (filled_size * fill_price))
    timestamp = order.get('datetime', time.strftime('%Y-%m-%d %H:%M:%S'))

    logger.info(f"[{label} EXECUTED] Symbol: {symbol} | Order ID: {order_id}")
    logger.info(f"     ├── Status:      {status}")
    logger.info(f"     ├── Filled Size: {filled_size:.4f} units")
    logger.info(f"     ├── Fill Price:  ${fill_price:.2f} USDC")
    logger.info(f"     ├── Total Cost:  ${total_cost:.2f} USDC")
    logger.info(f"     └── Time:        {timestamp}\n")


def execute_atomic_hedge(exchange, plan):
    """Executes chunked TWAP Leg A (Spot Long) and Leg B (Perp Short) entry orders."""
    spot_symbol = plan['spot_symbol']
    perp_symbol = plan['perp_symbol']
    total_quantity = float(plan['quantity'])

    logger.info("=" * 60)
    logger.info("      STAGE 3: ATOMIC HEDGE PLACEMENT (CHUNKED TWAP)     ")
    logger.info("=" * 60)
    logger.info(f"[*] Target Selected:   {perp_symbol} (Spot: {spot_symbol})")
    logger.info(f"[*] Current Price:     ${plan['current_price']:.2f}")
    logger.info(f"[*] Total Order Size:  {total_quantity} contract(s) (~${plan['estimated_notional']:.2f} USDC)")

    # Calculate slices based on the tightest exchange constraint (190 BNB)
    num_slices = math.ceil(total_quantity / MAX_SPOT_LIMIT)
    slice_size = total_quantity / num_slices
    logger.info(f"[*] Slicing order into {num_slices} chunk(s) of ~{slice_size:.4f} BNB each\n")

    filled_spot = 0.0
    filled_perp = 0.0

    try:
        for i in range(num_slices):
            current_chunk = min(slice_size, total_quantity - filled_spot)
            chunk_qty = float(exchange.amount_to_precision(spot_symbol, current_chunk))

            logger.info(f"--- [ENTRY SLICE {i+1}/{num_slices}] Target Chunk Size: {chunk_qty} BNB ---")

            # Leg A: Market Buy Spot Slice
            logger.info(f"[*] Submitting Leg A (Spot Buy Chunk {i+1})...")
            spot_order = exchange.create_market_buy_order(spot_symbol, chunk_qty)
            if spot_order.get('status') == 'open':
                spot_order = _wait_for_order_fill(exchange, spot_order['id'], spot_symbol)
            _format_order_fill_log(f"LEG A CHUNK {i+1}", spot_order, spot_symbol)
            filled_spot += chunk_qty

            # Leg B: Market Short Perp Slice
            logger.info(f"[*] Submitting Leg B (Perp Short Chunk {i+1})...")
            perp_order = exchange.create_market_sell_order(perp_symbol, chunk_qty)
            if perp_order.get('status') == 'open':
                perp_order = _wait_for_order_fill(exchange, perp_order['id'], perp_symbol)
            _format_order_fill_log(f"LEG B CHUNK {i+1}", perp_order, perp_symbol)
            filled_perp += chunk_qty

            if i < num_slices - 1:
                time.sleep(1.5)

        logger.info("[✓] STAGE 3 PASSED: Delta-Neutral Position Established Successfully!\n")
        return True

    except Exception as e:
        logger.critical(f"[!] Critical Error during atomic hedge execution: {e}", exc_info=True)
        return False


def execute_atomic_unwind(exchange, spot_symbol, perp_symbol, total_quantity):
    """Executes Stage 5 chunked TWAP unwind for Perp short closure and Spot inventory sale."""
    logger.info("\n" + "=" * 60)
    logger.info("       STAGE 5: EXECUTING CHUNKED ATOMIC UNWIND & EXIT       ")
    logger.info("=" * 60)

    num_slices = math.ceil(total_quantity / MAX_SPOT_LIMIT)
    slice_size = total_quantity / num_slices
    logger.info(f"[*] Unwinding total position of {total_quantity} BNB across {num_slices} chunk(s)...\n")

    unwound_spot = 0.0
    unwound_perp = 0.0

    try:
        for i in range(num_slices):
            current_chunk = min(slice_size, total_quantity - unwound_spot)
            chunk_qty = float(exchange.amount_to_precision(spot_symbol, current_chunk))

            logger.info(f"--- [UNWIND SLICE {i+1}/{num_slices}] Size: {chunk_qty} BNB ---")

            # Leg B Unwind: Close Futures Short Chunk (Reduce-Only)
            logger.info(f"[*] Closing Leg B Perp Chunk {i+1}...")
            try:
                perp_unwind = exchange.create_order(
                    symbol=perp_symbol,
                    type='market',
                    side='buy',
                    amount=chunk_qty,
                    params={'reduceOnly': True}
                )
                if perp_unwind.get('status') == 'open':
                    perp_unwind = _wait_for_order_fill(exchange, perp_unwind['id'], perp_symbol)
                _format_order_fill_log(f"LEG B UNWIND CHUNK {i+1}", perp_unwind, perp_symbol)
            except Exception as e:
                logger.warning(f"Standard reduce-only close failed on chunk {i+1} ({e}). Retrying standard buy...")
                perp_unwind = exchange.create_market_buy_order(perp_symbol, chunk_qty)
                _format_order_fill_log(f"LEG B UNWIND CHUNK {i+1} (FALLBACK)", perp_unwind, perp_symbol)
            
            unwound_perp += chunk_qty

            # Leg A Unwind: Sell Spot Inventory Chunk
            logger.info(f"[*] Closing Leg A Spot Chunk {i+1}...")
            spot_unwind = exchange.create_market_sell_order(spot_symbol, chunk_qty)
            if spot_unwind.get('status') == 'open':
                spot_unwind = _wait_for_order_fill(exchange, spot_unwind['id'], spot_symbol)
            _format_order_fill_log(f"LEG A UNWIND CHUNK {i+1}", spot_unwind, spot_symbol)
            
            unwound_spot += chunk_qty

            if i < num_slices - 1:
                time.sleep(1.5)

        logger.info("[✓] STAGE 5 PASSED: Delta-Neutral Position Unwound & Capital Secured!\n")
        return True

    except Exception as e:
        logger.critical(f"[!] Fatal Unwind Error: Position may be partially unhedged! Exception: {e}", exc_info=True)
        return False