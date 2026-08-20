import sys
import os

# Resolve project root and add to sys.path
package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if package_root not in sys.path:
    sys.path.insert(0, package_root)

import time
import msvcrt
import threading
import argparse
import ccxt
import config.settings as settings
from core.logger import setup_logger
from execution.risk import calculate_dynamic_stop_loss, calculate_estimated_entry_fees
from execution.trader import execute_atomic_unwind

logger = setup_logger()


def _listen_for_user_commands(command_state):
    """Background thread worker for non-blocking terminal inputs."""
    while command_state['running']:
        if msvcrt.kbhit():
            try:
                key = msvcrt.getch().decode('utf-8').lower()
                if key == 'e':
                    logger.warning("\n[USER COMMAND] Manual emergency exit requested ('E' pressed).")
                    command_state['manual_exit'] = True
                    break
            except UnicodeDecodeError:
                pass
        time.sleep(0.2)


def fetch_live_position_quantity(exchange, perp_symbol: str, default_qty: float = None) -> float:
    """
    Dynamically fetches the exact active contract size from open exchange positions.
    Falls back cleanly if no active position is found.
    """
    try:
        quote_asset = perp_symbol.split('/')[1].split(':')[0]
        positions = exchange.fetch_positions(symbols=[perp_symbol], params={'currency': quote_asset})
        for pos in positions:
            contracts = abs(float(pos.get('contracts', 0.0) or pos.get('size', 0.0)))
            if contracts > 0:
                logger.info(f"[✓] Dynamically detected open exchange position: {contracts} contracts.")
                return contracts
    except Exception as e:
        logger.warning(f"[*] Could not query live position contracts ({e}).")

    if default_qty is not None:
        return default_qty

    # Dynamic default fallback based on current ticker price (~$30 USDC allocation)
    try:
        ticker = exchange.fetch_ticker(perp_symbol)
        price = float(ticker['last'] or ticker['ask'])
        dynamic_qty = float(exchange.amount_to_precision(perp_symbol, 30.0 / price))
        logger.info(f"[*] Dynamic fallback sizing calculated: {dynamic_qty} (~$30.00 USDC notional).")
        return dynamic_qty
    except Exception:
        return 0.05  # Safe micro-lot fallback


def monitor_risk_shield_stage_5(exchange, plan):
    """Monitors live position health using dynamic risk parameters, fee modeling, and tabular output."""
    logger.info("=" * 60)
    logger.info("      STAGE 4 & 5: AUTOMATED RISK SHIELD & EXIT MONITORING   ")
    logger.info("=" * 60)

    spot_symbol = plan['spot_symbol']
    perp_symbol = plan['perp_symbol']
    quantity = float(plan['quantity'])
    base_asset = spot_symbol.split('/')[0]
    quote_asset = spot_symbol.split('/')[1].split(':')[0]

    # Fetch initial prices at monitoring start to anchor entry basis
    try:
        init_spot = float(exchange.fetch_ticker(spot_symbol)['last'])
        init_perp = float(exchange.fetch_ticker(perp_symbol)['last'])
        entry_basis = (init_perp - init_spot) / init_spot if init_spot > 0 else 0.0
    except Exception as e:
        logger.warning(f"Could not establish initial entry basis: {e}. Falling back to 0.0 basis.")
        init_spot, init_perp = 0.0, 0.0
        entry_basis = 0.0

    # Dynamic Calculations
    notional_position_usdc = quantity * (init_perp or 500.0)
    estimated_entry_fees = calculate_estimated_entry_fees(notional_position_usdc)
    max_loss_usdc = calculate_dynamic_stop_loss(notional_position_usdc)

    start_time = time.time()

    logger.info(f"[*] Active Hedged Position: {quantity} {base_asset} (~${notional_position_usdc:.2f} {quote_asset})")
    logger.info(f"[*] Dynamic Friction Baseline:")
    logger.info(f"    ├── Est. Entry Trading Fees: -${estimated_entry_fees:.2f} {quote_asset}")
    logger.info(f"    └── Initial Basis Spread:    +${abs(init_perp - init_spot):.2f} {quote_asset}")
    logger.info(f"[*] Dynamic Risk Thresholds:")
    logger.info(f"    ├── Dynamic Stop Loss:      -${max_loss_usdc:.2f} {quote_asset}")
    logger.info(f"    ├── Max Allowable Drift:     {getattr(settings, 'MAX_DRIFT_PCT', 5.0):.2f}% (Basis Drift)")
    logger.info(f"    └── Exit Trigger:           Negative Funding Rate")
    logger.info("[*] Controls: Press 'E' to trigger manual unwind | Ctrl+C to abort monitoring.\n")

    # Tabular Header
    logger.info(
        f"{'TIME':<8} | {'SPOT':<8} | {'PERP':<8} | {'RATE':<8} | {'8H YIELD':<10} | {'UPNL':<8} | {'NET PNL':<10} | {'BE IN':<8} | {'STOP':<10}"
    )
    logger.info("-" * 102)

    command_state = {'running': True, 'manual_exit': False}
    input_thread = threading.Thread(
        target=_listen_for_user_commands, 
        args=(command_state,), 
        daemon=True
    )
    input_thread.start()

    try:
        while command_state['running']:
            if command_state['manual_exit']:
                logger.warning("[!] Initiating Stage 5 Emergency Exit from user request...")
                command_state['running'] = False
                return execute_atomic_unwind(exchange, spot_symbol, perp_symbol, quantity)

            try:
                spot_ticker = exchange.fetch_ticker(spot_symbol)
                perp_ticker = exchange.fetch_ticker(perp_symbol)

                spot_price = float(spot_ticker['last'])
                perp_price = float(perp_ticker['last'])

                # Recalculate dynamic notional value with live pricing
                current_notional = quantity * perp_price
                current_max_loss = calculate_dynamic_stop_loss(current_notional)

                raw_info = perp_ticker.get('info', {})
                current_8h_rate = float(raw_info.get('current_funding', 0.0) or 0.0) * 100

                current_basis = (perp_price - spot_price) / spot_price if spot_price > 0 else 0.0
                drift_pct = abs(current_basis - entry_basis) * 100

                unrealized_pnl = 0.0
                try:
                    positions = exchange.fetch_positions(
                        symbols=[perp_symbol], 
                        params={'currency': quote_asset}
                    )
                    for pos in positions:
                        if abs(float(pos.get('contracts', 0))) > 0:
                            unrealized_pnl = float(pos.get('unrealizedPnl', 0.0))
                            break
                except Exception:
                    pass

                expected_8h_payout = current_notional * (current_8h_rate / 100)
                elapsed_hours = (time.time() - start_time) / 3600
                accrued_payouts_count = int(elapsed_hours / 8)
                est_gross_funding_accrued = accrued_payouts_count * expected_8h_payout

                net_pnl = unrealized_pnl - estimated_entry_fees + est_gross_funding_accrued

                intervals_to_breakeven = (
                    max(0, (estimated_entry_fees - unrealized_pnl) / expected_8h_payout)
                    if expected_8h_payout > 0 else 0
                )

                # Formatted Tabular Row
                logger.info(
                    f"{time.strftime('%H:%M:%S'):<8} | "
                    f"${spot_price:<7.2f} | "
                    f"${perp_price:<7.2f} | "
                    f"{current_8h_rate:>7.4f}% | "
                    f"+${expected_8h_payout:<8.2f} | "
                    f"${unrealized_pnl:>+7.2f} | "
                    f"${net_pnl:>+9.2f} | "
                    f"~{intervals_to_breakeven:<4.1f} pks | "
                    f"-${current_max_loss:<8.2f}"
                )

                exit_reason = None
                if current_8h_rate < 0:
                    exit_reason = f"Funding rate flipped negative ({current_8h_rate:.4f}%)."
                elif drift_pct >= getattr(settings, 'MAX_DRIFT_PCT', 5.0):
                    exit_reason = f"Basis drift ({drift_pct:.2f}%) exceeded threshold ({getattr(settings, 'MAX_DRIFT_PCT', 5.0)}%)."
                elif unrealized_pnl <= -current_max_loss:
                    exit_reason = f"Unrealized loss (-${abs(unrealized_pnl):.2f}) hit dynamic stop loss (-${current_max_loss:.2f})."

                if exit_reason:
                    logger.error(f"\n[!] RISK SHIELD TRIP: {exit_reason}")
                    logger.error("[!] Initiating Stage 5 Emergency Exit...")
                    command_state['running'] = False
                    return execute_atomic_unwind(exchange, spot_symbol, perp_symbol, quantity)

            except (ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as net_err:
                logger.warning(f"Transient API warning: {net_err}. Retrying in {getattr(settings, 'CHECK_INTERVAL_SEC', 10)}s...")

            time.sleep(getattr(settings, 'CHECK_INTERVAL_SEC', 10))

    except KeyboardInterrupt:
        logger.info("\n[*] Manual exit: Stopped Risk Shield monitoring via Ctrl+C.")
        command_state['running'] = False
        return False
    except Exception as e:
        logger.critical(f"\n[!] Fatal Risk Shield Exception: {e}", exc_info=True)
        command_state['running'] = False
        return False
    finally:
        command_state['running'] = False


def main():
    parser = argparse.ArgumentParser(description='Dynamic standalone risk monitor.')
    parser.add_argument('--spot', default='BNB/USDC', help='Spot symbol to monitor.')
    parser.add_argument('--perp', default='BNB/USDC:USDC', help='Perpetual symbol to monitor.')
    parser.add_argument('--quantity', type=float, default=None, help='Explicit quantity override.')
    args = parser.parse_args()

    try:
        from main import init_exchange
    except Exception as e:
        logger.critical(f"Failed to import init_exchange from main.py: {e}")
        return

    exchange = init_exchange()

    quantity = args.quantity
    if quantity is None:
        quantity = fetch_live_position_quantity(exchange, args.perp)

    plan = {
        'spot_symbol': args.spot,
        'perp_symbol': args.perp,
        'quantity': quantity,
    }

    logger.info('[*] Starting dynamic risk monitor with plan: %s', plan)
    monitor_risk_shield_stage_5(exchange, plan)


if __name__ == '__main__':
    main()