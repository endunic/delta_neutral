import sys
import os
import argparse

# Get the absolute path to the delta_neutral package directory
package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Add the delta_neutral package path to sys.path so sibling packages resolve correctly
if package_root not in sys.path:
    sys.path.insert(0, package_root)

import config.settings as settings


def calculate_dynamic_stop_loss(notional_value_usdc: float) -> float:
    """Calculates dynamic stop-loss threshold based on total position notional size."""
    dynamic_stop = notional_value_usdc * getattr(settings, 'MAX_LOSS_PCT', 0.006)
    min_floor = getattr(settings, 'MIN_STOP_LOSS_USDC', 10.0)
    return max(dynamic_stop, min_floor)


def calculate_estimated_entry_fees(notional_value_usdc: float) -> float:
    """Calculates combined estimated taker entry fees across both legs (Spot + Perp)."""
    fee_rate = getattr(settings, 'ESTIMATED_TAKER_FEE_RATE', 0.0005)
    return (notional_value_usdc * fee_rate) * 2.0


def format_monitoring_telemetry(unrealized_pnl_usdc: float, notional_value_usdc: float, funding_yield_accrued: float) -> str:
    """
    Formats compact single-line monitoring telemetry for clean terminal rendering.
    Calculates True Net PnL considering round-trip fee friction.
    """
    max_loss_usdc = calculate_dynamic_stop_loss(notional_value_usdc)
    entry_fees = calculate_estimated_entry_fees(notional_value_usdc)
    
    # Round-trip fee friction (Entry fees + estimated Exit fees)
    total_fee_friction = entry_fees * 2.0
    
    # True bottom-line net PnL accounting for accrued funding and fees
    true_net_pnl = unrealized_pnl_usdc + funding_yield_accrued - total_fee_friction

    # Compact output format ~75 characters total
    return (
        f"[MONITOR] UPnL:${unrealized_pnl_usdc:+.2f} | "
        f"Fund:+${funding_yield_accrued:.2f} | "
        f"Fees:-${total_fee_friction:.2f} | "
        f"Net:${true_net_pnl:+.2f} | "
        f"Stop:-${max_loss_usdc:.2f}"
    )


def evaluate_risk_shield(unrealized_pnl_usdc: float, notional_value_usdc: float, initial_basis_pct: float, current_basis_pct: float) -> dict:
    """Evaluates position state against dynamic risk limits."""
    max_loss_usdc = calculate_dynamic_stop_loss(notional_value_usdc)
    max_drift_pct = getattr(settings, 'MAX_DRIFT_PCT', 5.0)

    # 1. Stop-Loss Trigger Check
    if unrealized_pnl_usdc <= -max_loss_usdc:
        return {
            'should_exit': True,
            'reason': f"Max dynamic loss threshold breached: ${unrealized_pnl_usdc:.2f} <= -${max_loss_usdc:.2f}"
        }

    # 2. Basis Spread Drift Check
    basis_drift = abs(current_basis_pct - initial_basis_pct)
    if basis_drift >= max_drift_pct:
        return {
            'should_exit': True,
            'reason': f"Basis spread drift limit breached: {basis_drift:.2f}% >= {max_drift_pct:.2f}%"
        }

    return {'should_exit': False, 'reason': "Nominal operational parameters"}


def main():
    parser = argparse.ArgumentParser(description='Standalone risk helper for delta_neutral execution.')
    parser.add_argument('--notional', type=float, default=1000.0, help='Notional value in USDC')
    parser.add_argument('--upnl', type=float, default=0.0, help='Unrealized PnL in USDC')
    parser.add_argument('--funding', type=float, default=0.0, help='Funding yield accrued in USDC')
    parser.add_argument('--initial-basis', type=float, default=0.0, help='Initial basis percentage')
    parser.add_argument('--current-basis', type=float, default=0.0, help='Current basis percentage')
    args = parser.parse_args()

    stop_loss = calculate_dynamic_stop_loss(args.notional)
    formatted = format_monitoring_telemetry(args.upnl, args.notional, args.funding)
    evaluation = evaluate_risk_shield(args.upnl, args.notional, args.initial_basis, args.current_basis)

    print(f"Notional: ${args.notional:.2f}")
    print(f"Calculated stop loss: ${stop_loss:.2f}")
    print(formatted)
    print(f"Risk evaluation: {evaluation}")


if __name__ == '__main__':
    main()