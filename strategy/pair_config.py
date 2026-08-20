"""
Cached Multi-Profile Strategy Runner
Fetches live exchange data once, then evaluates multiple capital tiers without extra network calls.
"""

import sys
from pathlib import Path

# =====================================================================
# DYNAMIC PATH RESOLUTION (Fixes ModuleNotFoundError)
# =====================================================================
# Resolves the project root directory (.../project_09) and prepends it to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# =====================================================================

from typing import List, Dict, Any
from delta_neutral.strategy.pair_selector import fetch_market_snapshot, evaluate_pairs


CAPITAL_PROFILES: List[Dict[str, Any]] = [
    {
        "name": "Micro Account ($10)",
        "balance": 10.0,
        "max_alloc_pct": 0.30,
        "max_friction_pct": 0.25,
        "min_funding_pct": 0.0,
    },
    {
        "name": "Small Account ($30)",
        "balance": 30.0,
        "max_alloc_pct": 0.20,
        "max_friction_pct": 0.25,
        "min_funding_pct": 0.0,
    },
    {
        "name": "Standard Account ($50)",
        "balance": 50.0,
        "max_alloc_pct": 0.15,
        "max_friction_pct": 0.20,
        "min_funding_pct": 0.01,
    },
]


def run_cached_multi_profile_scan():
    print("===========================================================================")
    print("      DELTA-NEUTRAL CACHED MULTI-PROFILE SCANNER (SINGLE API CALL)         ")
    print("===========================================================================\n")

    # Step 1: Fetch ticker data ONCE from exchange
    market_snapshot = fetch_market_snapshot()

    if not market_snapshot:
        print("[!] Aborting scan: Failed to retrieve market data.")
        return

    # Step 2: Loop through profiles locally using cached data
    for idx, profile in enumerate(CAPITAL_PROFILES, 1):
        print(f"[{idx}/{len(CAPITAL_PROFILES)}] EVALUATING PROFILE: {profile['name'].upper()}")
        print(f"    ├── Account Capital: ${profile['balance']:.2f} USDC")
        print(f"    ├── Max Position Size: ${profile['balance'] * profile['max_alloc_pct']:.2f} USDC ({profile['max_alloc_pct'] * 100:.0f}%)")
        print(f"    └── Friction Limit: {profile['max_friction_pct']}%\n")

        evaluate_pairs(
            market_data=market_snapshot,
            account_balance=profile["balance"],
            max_allocation_pct=profile["max_alloc_pct"],
            max_friction_pct=profile["max_friction_pct"],
            min_funding_pct=profile["min_funding_pct"]
        )

        print("\n" + "-" * 88 + "\n")


if __name__ == "__main__":
    run_cached_multi_profile_scan()