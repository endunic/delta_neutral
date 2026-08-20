import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =====================================================================
# API & EXCHANGE CONFIGURATION
# =====================================================================
DERIBIT_API_KEY = os.getenv("DERIBIT_API_KEY", "")
DERIBIT_API_SECRET = os.getenv("DERIBIT_API_SECRET", "")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# =====================================================================
# RISK MANAGEMENT & MONITORING THRESHOLDS
# =====================================================================
# Percentage of free available USDC balance to allocate per arbitrage trade
CAPITAL_ALLOCATION_PCT = 0.80  # 80% allocation

# Dynamic Stop-Loss Threshold: 0.6% (0.006) of position notional value
# Example: $8,000 position * 0.006 = $48.00 max allowable loss
MAX_LOSS_PCT = 0.006  

# Minimum stop-loss floor in USDC for smaller trade sizes
MIN_STOP_LOSS_USDC = 10.0

# Maximum allowable percentage drift in Spot-Perp basis spread
MAX_DRIFT_PCT = 5.0  

# Monitoring loop sleep interval (in seconds)
CHECK_INTERVAL_SEC = 10.0

# Estimated Taker Fee Rate per leg (Deribit ~0.05% = 0.0005)
ESTIMATED_TAKER_FEE_RATE = 0.0005