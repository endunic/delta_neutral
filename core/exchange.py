import ccxt
import config.settings as settings

def initialize_deribit(authenticated=True):
    """Initializes and configures the CCXT Deribit client."""
    config = {
        'enableRateLimit': True,
        'options': {
            'defaultType': 'swap',
        }
    }
    
    if authenticated:
        if not settings.API_KEY or not settings.API_SECRET:
            raise ValueError("[!] Export DERIBIT_TESTNET_KEY and DERIBIT_TESTNET_SECRET in terminal.")
        config['apiKey'] = settings.API_KEY
        config['secret'] = settings.API_SECRET

    exchange = ccxt.deribit(config)
    exchange.set_sandbox_mode(True)
    return exchange
