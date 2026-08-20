import logging
import os
import sys

# Force UTF-8 on Windows console to prevent Unicode / cp1252 character crashes
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')


class ColorFormatter(logging.Formatter):
    """Custom color-coded console log formatter using native ANSI escape sequences."""
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    RESET = "\033[0m"

    FORMATS = {
        logging.DEBUG: CYAN + "%(asctime)s [%(levelname)s] %(message)s" + RESET,
        logging.INFO: WHITE + "%(asctime)s [%(levelname)s] %(message)s" + RESET,
        logging.WARNING: YELLOW + "%(asctime)s [%(levelname)s] %(message)s" + RESET,
        logging.ERROR: RED + "%(asctime)s [%(levelname)s] %(message)s" + RESET,
        logging.CRITICAL: RED + "%(asctime)s [%(levelname)s] %(message)s" + RESET,
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, "%(asctime)s [%(levelname)s] %(message)s")
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)


def setup_logger():
    os.makedirs('logs', exist_ok=True)
    logger = logging.getLogger('DeltaNeutral')
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        c_handler = logging.StreamHandler(sys.stdout)
        c_handler.setLevel(logging.INFO)
        c_handler.setFormatter(ColorFormatter())
        # Enforce standard \n terminator on stdout stream to stop line wrapping bugs
        c_handler.terminator = "\n"
        logger.addHandler(c_handler)

        f_handler = logging.FileHandler('logs/engine_execution.log', encoding='utf-8')
        f_handler.setLevel(logging.DEBUG)
        f_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(module)s: %(message)s')
        f_handler.setFormatter(f_formatter)
        logger.addHandler(f_handler)

    return logger