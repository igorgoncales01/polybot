#!/usr/bin/env python3
"""Dry-run: scan markets without authentication to verify connectivity."""

import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("test_scan")

from scanner import scan_markets
from config import MIN_PRICE, MAX_PRICE, MIN_LIQUIDITY, TARGET_CATEGORIES

logger.info("Polybot Scanner Test")
logger.info("Filters: price %.0f-%.0f¢ | min liquidity $%.0f | categories: %s",
            MIN_PRICE * 100, MAX_PRICE * 100, MIN_LIQUIDITY, TARGET_CATEGORIES)
logger.info("-" * 70)

candidates = scan_markets()

if not candidates:
    logger.warning("No candidates found. This could mean:")
    logger.warning("  - No markets match the filters right now")
    logger.warning("  - API response format changed")
    logger.warning("  - Network issue")
else:
    candidates.sort(key=lambda c: c.price)
    logger.info("Found %d underdog candidates:\n", len(candidates))
    for i, c in enumerate(candidates[:20], 1):
        logger.info(
            "  %2d. %s — %s @ %.1f¢  (liq=$%.0f, cat=%s)",
            i, c.outcome, c.question[:55], c.price * 100, c.liquidity, c.category,
        )

logger.info("-" * 70)
logger.info("Done. Scanner is working.")
