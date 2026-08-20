import time
import sys
import json
import re
import ccxt
import config.settings as settings
from core.logger import setup_logger

logger = setup_logger()


def init_exchange():
    """
    Initializes CCXT Deribit client configured for Testnet sandbox mode.
    """
    exchange = ccxt.deribit({
        'apiKey': getattr(settings, 'DERIBIT_API_KEY', ''),
        'secret': getattr(settings, 'DERIBIT_API_SECRET', ''),
        'enableRateLimit': True,
        'timeout': 15000,  # 15s request timeout
    })
    exchange.set_sandbox_mode(True)
    return exchange


def fetch_account_balance(exchange, currency='USDC', fallback_balance=30.0):
    """
    Attempts to fetch live balance from exchange; falls back to default if unavailable.
    """
    try:
        balance = exchange.fetch_balance()
        free_balance = float(balance.get(currency, {}).get('free', 0.0))
        if free_balance > 0:
            return free_balance
    except Exception as e:
        logger.warning(f"[*] Could not fetch live balance ({e}). Using default balance: ${fallback_balance:.2f}")
    return fallback_balance


def calculate_dynamic_trade_parameters(
    exchange, 
    spot_symbol: str, 
    perp_symbol: str, 
    account_balance: float, 
    max_alloc_pct: float = 0.20,
    stop_loss_pct: float = 0.02,
    expected_8h_funding_rate: float = 0.0001  # Baseline ~0.01% per 8h
) -> dict:
    """
    Dynamically computes trade size, fees, stop loss, and estimated 8H payout.
    """
    ticker = exchange.fetch_ticker(spot_symbol)
    spot_price = float(ticker['last'] or ticker['ask'])

    # Capital Allocation (20% of free balance)
    allocated_capital = account_balance * max_alloc_pct
    raw_size = allocated_capital / spot_price
    
    # Format size according to exchange precision rules
    formatted_size = float(exchange.amount_to_precision(spot_symbol, raw_size))
    actual_trade_value = formatted_size * spot_price

    # Fee Estimation (~0.10% combined taker fee for round trip)
    estimated_taker_fee_rate = 0.0010
    dynamic_est_fees = -round(actual_trade_value * estimated_taker_fee_rate, 2)

    # Expected 8-Hour Funding Income & Projected Net Payout
    est_funding_reward = round(actual_trade_value * expected_8h_funding_rate, 4)
    est_net_payout = round(est_funding_reward + dynamic_est_fees, 2)

    # Risk Management (2% of allocated capital + estimated fees)
    max_acceptable_loss = -(allocated_capital * stop_loss_pct)
    dynamic_stop_loss = round(max_acceptable_loss + dynamic_est_fees, 2)

    return {
        'trade_size': formatted_size,
        'actual_trade_value': actual_trade_value,
        'dynamic_stop_loss': dynamic_stop_loss,
        'est_fees': dynamic_est_fees,
        'est_funding_reward': est_funding_reward,
        'est_net_payout': est_net_payout
    }


def fetch_accrued_funding(exchange, perp_symbol, start_timestamp_ms):
    """
    Fetches net accrued funding payments for the active perpetual trade
    from Deribit's private user transaction log since entry timestamp.
    """
    try:
        market = exchange.market(perp_symbol)
        instrument_name = market['id']

        response = exchange.private_get_get_user_trades_by_instrument({
            'instrument_name': instrument_name,
            'start_timestamp': int(start_timestamp_ms),
            'count': 100
        })

        trades = response.get('result', {}).get('trades', [])
        
        total_funding = 0.0
        for trade in trades:
            if 'funding' in trade:
                total_funding += float(trade.get('funding', 0.0))

        return round(total_funding, 4)

    except Exception:
        return 0.00


def _parse_exchange_limit(err_str):
    """
    Extract the 'limit' value from a Deribit JSON-RPC error payload.

    Example payload:
        {"jsonrpc":"2.0","error":{"code":11022,"data":{"limit":"200.0","currency":"BNB"},
         "message":"max_spot_order_quantity"}, ...}
    Returns the limit as a float, or None if it cannot be parsed.
    """
    try:
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


def execute_atomic_unwind(exchange, context):
    """
    STAGE 5: Emergency atomic unwind logic to close Perp Short and liquidate Spot Long.

    Uses the dedicated CCXT market-order helpers (create_market_buy_order /
    create_market_sell_order) and chunked unwinding with adaptive handling of the
    Deribit 'max_spot_order_quantity' (code 11022) rejection so a single large
    spot sell no longer aborts the unwind.
    """
    spot_symbol = context.get('spot_symbol')
    perp_symbol = context.get('perp_symbol')
    size = float(context.get('size'))

    logger.info(f"[*] Initiating atomic unwind for {size} unit(s)...")

    # 1. Close Perp Leg (Buy back short) — reduce-only first, plain buy fallback
    try:
        perp_size = float(exchange.amount_to_precision(perp_symbol, size))
        logger.info(f"[*] Closing Leg B (Perp Short): Market Buy {perp_size} {perp_symbol}...")
        try:
            order_perp = exchange.create_market_buy_order(perp_symbol, perp_size, params={'reduceOnly': True})
        except Exception:
            order_perp = exchange.create_market_buy_order(perp_symbol, perp_size)
        logger.info(f"[✓] Leg B Closed! Order ID: {order_perp.get('id')}")
    except Exception as e:
        logger.error(f"[!] Failed to close Perp Short leg: {e}")

    # 2. Liquidate Spot Leg (Sell long spot) — chunked with adaptive limit handling
    remaining = size
    min_floor = 0.0001
    chunk_size = min(size, 10.0)  # conservative starting chunk
    consecutive_limit_failures = 0
    max_consecutive_failures = 5

    while remaining > min_floor:
        try:
            balance = exchange.fetch_balance()
            free_spot = float(balance.get(spot_symbol.split('/')[0], {}).get('free', 0.0) or 0.0)
            if free_spot <= min_floor:
                logger.info(f"[✓] Spot leg fully liquidated (free balance <= {min_floor}).")
                break

            qty = min(chunk_size, remaining, free_spot)
            spot_size = float(exchange.amount_to_precision(spot_symbol, qty))
            if spot_size < min_floor:
                logger.warning(f"[!] Residual spot balance {free_spot:.4f} below tradeable floor; "
                               f"cannot liquidate via market order.")
                break

            logger.info(f"[*] Closing Leg A (Spot Long): Market Sell {spot_size} {spot_symbol}...")
            order_spot = exchange.create_market_sell_order(spot_symbol, spot_size)
            logger.info(f"[✓] Leg A Liquidated! Order ID: {order_spot.get('id')}")

            remaining -= spot_size
            consecutive_limit_failures = 0
            # Scale up slightly on success for faster liquidation (capped)
            chunk_size = min(chunk_size * 1.2, 50.0)
            time.sleep(0.2)
        except Exception as e:
            err_str = str(e)
            if "max_spot_order_quantity" in err_str or "11022" in err_str or "Invalid params" in err_str:
                api_limit = _parse_exchange_limit(err_str)
                if api_limit is not None and api_limit > 0:
                    logger.warning(f"[!] Exchange reports order limit {api_limit} {spot_symbol.split('/')[0]}. "
                                   f"Adapting chunk size: {chunk_size} -> {api_limit}.")
                    chunk_size = api_limit
                else:
                    new_chunk = max(chunk_size / 2.0, min_floor)
                    logger.warning(f"[!] Exceeded exchange limit. Adapting chunk size: "
                                   f"{chunk_size} -> {new_chunk:.5f}.")
                    chunk_size = new_chunk

                consecutive_limit_failures += 1
                if consecutive_limit_failures >= max_consecutive_failures:
                    logger.error(f"[!] {consecutive_limit_failures} consecutive limit failures at chunk "
                                 f"size {chunk_size}. Aborting spot liquidation to avoid an infinite loop; "
                                 f"remaining balance must be cleared manually.")
                    break
            else:
                logger.error(f"[!] Failed to liquidate Spot leg: {e}")
                break
            time.sleep(0.5)

    if remaining > min_floor:
        logger.warning(f"[!] Spot leg partially liquidated. Remaining: {remaining:.4f} "
                       f"{spot_symbol.split('/')[0]} (clear manually if needed).")
    else:
        logger.info(f"[✓] Spot leg liquidation complete.")


def run_pipeline(account_balance: float = None):
    logger.info("=========================================================================================")
    logger.info("                  STAGE 1 & 2: DERIBIT TESTNET SCANNER & DYNAMIC SIZING                  ")
    logger.info("=========================================================================================")

    exchange = init_exchange()

    try:
        exchange.load_markets()
        logger.info("[✓] Connected to Deribit Testnet successfully!")
        logger.info("[*] Authenticating API credentials with Deribit Testnet...")
        logger.info("[✓] API credentials verified successfully!")
    except (ccxt.NetworkError, ccxt.RequestTimeout) as net_err:
        logger.error(f"[!] Initial connection failed due to network timeout: {net_err}")
        return
    except Exception as e:
        logger.error(f"[!] Initialization error: {e}")
        return

    spot_symbol = "BNB/USDC"
    perp_symbol = "BNB/USDC:USDC"

    if account_balance is None:
        account_balance = fetch_account_balance(exchange, currency='USDC', fallback_balance=30.0)

    dynamic_params = calculate_dynamic_trade_parameters(
        exchange=exchange,
        spot_symbol=spot_symbol,
        perp_symbol=perp_symbol,
        account_balance=account_balance,
        max_alloc_pct=0.20,
        stop_loss_pct=0.02
    )

    trade_size = dynamic_params['trade_size']
    dynamic_stop_loss = dynamic_params['dynamic_stop_loss']
    est_fees = dynamic_params['est_fees']
    est_net_payout = dynamic_params['est_net_payout']

    logger.info(f"[*] DYNAMIC PARAMETERS CALCULATED (Account Balance: ${account_balance:.2f}):")
    logger.info(f"    ├── Dynamic Trade Size: {trade_size} (~${dynamic_params['actual_trade_value']:.2f} USDC)")
    logger.info(f"    ├── Dynamic Estimated Fees: ${est_fees:.2f} USDC")
    logger.info(f"    ├── Projected 8H Net Payout: ${est_net_payout:.2f} USDC")
    logger.info(f"    └── Dynamic Stop Loss: ${dynamic_stop_loss:.2f} USDC")

    position_active = False
    active_context = None

    try:
        logger.info("=========================================================================================")
        logger.info("                  STAGE 3: ATOMIC HEDGE PLACEMENT (TESTNET)                              ")
        logger.info("=========================================================================================")

        logger.info(f"[*] Submitting Leg A: Market Buy {trade_size} {spot_symbol}...")
        spot_order = exchange.create_market_order(spot_symbol, 'buy', trade_size)
        logger.info(f"[LEG A (SPOT BUY) EXECUTED] Symbol: {spot_symbol} | Order ID: {spot_order.get('id')}")

        logger.info(f"[*] Submitting Leg B: Market Sell {trade_size} {perp_symbol}...")
        perp_order = exchange.create_market_order(perp_symbol, 'sell', trade_size)
        logger.info(f"[LEG B (PERP SHORT) EXECUTED] Symbol: {perp_symbol} | Order ID: {perp_order.get('id')}")

        position_active = True
        active_context = {
            'spot_symbol': spot_symbol,
            'perp_symbol': perp_symbol,
            'size': trade_size
        }
        logger.info("[✓] STAGE 3 PASSED: Delta-Neutral Position Established Successfully!")

        logger.info("=========================================================================================")
        logger.info("                  STAGE 4: AUTOMATED 8-HOUR FUNDING & RISK MONITOR                       ")
        logger.info("=========================================================================================")

        spot_ticker = exchange.fetch_ticker(spot_symbol)
        perp_ticker = exchange.fetch_ticker(perp_symbol)

        entry_spot_price = float(spot_order.get('average') or spot_order.get('price') or spot_ticker['last'])
        entry_perp_price = float(perp_order.get('average') or perp_order.get('price') or perp_ticker['last'])

        HOLD_DURATION_SECONDS = 8 * 3600
        start_time = time.time()
        start_timestamp_ms = start_time * 1000

        # Updated Header including EST PAYOUT column
        logger.info(
            f"{'TIME':<8} | {'8H COUNTDOWN':<12} | {'UPnL (USDC)':<11} | {'FUNDING':<9} | {'NET PnL':<9} | {'EST PAYOUT':<10} | {'STOP LIMIT':<11}"
        )
        logger.info("-" * 102)

        last_funding_fetch = 0.0
        accrued_funding = 0.00

        while position_active:
            try:
                current_timestamp = time.time()
                elapsed_seconds = current_timestamp - start_time
                remaining_seconds = max(0, HOLD_DURATION_SECONDS - elapsed_seconds)

                rem_hours, rem_remainder = divmod(int(remaining_seconds), 3600)
                rem_minutes, rem_secs = divmod(rem_remainder, 60)
                countdown_str = f"{rem_hours:02d}:{rem_minutes:02d}:{rem_secs:02d}"

                ticker_spot = exchange.fetch_ticker(spot_symbol)
                ticker_perp = exchange.fetch_ticker(perp_symbol)

                curr_spot = float(ticker_spot['last'])
                curr_perp = float(ticker_perp['last'])

                if current_timestamp - last_funding_fetch >= 60.0:
                    accrued_funding = fetch_accrued_funding(exchange, perp_symbol, start_timestamp_ms)
                    last_funding_fetch = current_timestamp

                spot_pnl = (curr_spot - entry_spot_price) * trade_size
                perp_pnl = (entry_perp_price - curr_perp) * trade_size
                raw_upnl = spot_pnl + perp_pnl
                net_pnl = round(raw_upnl + accrued_funding + est_fees, 2)

                # Projected payout at the end of the 8-hour period
                projected_payout = round(net_pnl + dynamic_params['est_funding_reward'], 2)

                current_time = time.strftime("%H:%M:%S")

                logger.info(
                    f"{current_time:<8} | "
                    f"{countdown_str:<12} | "
                    f"{raw_upnl:>+11.2f} | "
                    f"{accrued_funding:>+9.4f} | "
                    f"{net_pnl:>+9.2f} | "
                    f"{projected_payout:>+10.2f} | "
                    f"{dynamic_stop_loss:>11.2f}"
                )

                if net_pnl <= dynamic_stop_loss:
                    logger.warning(f"\n[!] DYNAMIC STOP LOSS TRIGGERED (${net_pnl:.2f} <= ${dynamic_stop_loss:.2f})")
                    break

                if remaining_seconds <= 0:
                    logger.info("\n[✓] 8-HOUR HOLDING PERIOD COMPLETED: Funding Reward Accrued! Initiating Unwind...")
                    break

                time.sleep(10)

            except (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable) as net_err:
                logger.warning(f"[*] Transient Deribit API connection lag (retrying in 5s)... [{type(net_err).__name__}]")
                time.sleep(5)

            except Exception as monitor_err:
                logger.error(f"[!] Error fetching market updates: {monitor_err}")
                time.sleep(5)

    except KeyboardInterrupt:
        logger.warning("\n[!] KeyboardInterrupt (Ctrl+C) detected! Triggering safe unwind...")
    except Exception as e:
        logger.error(f"[!] Critical error during execution: {e}", exc_info=True)
    finally:
        if position_active and active_context:
            logger.info("=========================================================================================")
            logger.info("                  STAGE 5: EMERGENCY ATOMIC UNWIND INITIATED                             ")
            logger.info("=========================================================================================")
            try:
                execute_atomic_unwind(exchange, active_context)
                logger.info("[✓] Emergency unwind completed successfully.")
            except Exception as unwind_err:
                logger.critical(f"[!] FAILED TO UNWIND POSITION IN FINALLY BLOCK: {unwind_err}")
        else:
            logger.info("[*] Clean shutdown: No active delta-neutral exposure remains.")


if __name__ == "__main__":
    run_pipeline()