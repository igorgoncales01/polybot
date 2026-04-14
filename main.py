#!/usr/bin/env python3
"""
Polybot – Underdog Bounce Strategy
Main loop: scan → enter → monitor exits → repeat.
"""

import sys
import time
import logging
from logging.handlers import RotatingFileHandler

from config import SCAN_INTERVAL, LOG_FILE, LOG_LEVEL
from auth import ClobAuth
from scanner import scan_markets
from executor import Executor


def setup_logging() -> logging.Logger:
    """Configure logging to both file and console."""
    root = logging.getLogger("polybot")
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler (5 MB × 3 backups)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    return root


def main() -> None:
    logger = setup_logging()
    logger.info("=" * 60)
    logger.info("Polybot Underdog Bounce starting")
    logger.info("=" * 60)

    # Authenticate
    try:
        auth = ClobAuth()
        auth.derive_api_key()
    except Exception as exc:
        logger.critical("Authentication failed: %s", exc)
        sys.exit(1)

    executor = Executor(auth)
    cycle = 0

    while True:
        cycle += 1
        logger.info("── Cycle %d ──", cycle)

        try:
            # 1. Check exits on existing positions
            executor.check_exits()

            # 2. Scan for new candidates
            candidates = scan_markets()
            already_held = set(executor.positions.keys())

            new_candidates = [
                c for c in candidates if c.token_id not in already_held
            ]

            if new_candidates:
                logger.info(
                    "New candidates: %d (showing top 5)",
                    len(new_candidates),
                )
                # Sort by lowest price (biggest underdog first)
                new_candidates.sort(key=lambda c: c.price)
                for cand in new_candidates[:5]:
                    logger.info(
                        "  → %s [%s] @ %.1f¢  liq=$%.0f",
                        cand.outcome,
                        cand.category,
                        cand.price * 100,
                        cand.liquidity,
                    )

                # 3. Open positions on best candidates
                for cand in new_candidates:
                    executor.open_position(cand)
            else:
                logger.info("No new candidates this cycle")

            # 4. Status
            logger.info(executor.status_summary())

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.exception("Error in cycle %d: %s", cycle, exc)

        # 5. Wait
        logger.debug("Sleeping %ds...", SCAN_INTERVAL)
        try:
            time.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            break

    # Graceful shutdown
    logger.info("Shutting down. Open positions: %d", len(executor.positions))
    if executor.positions:
        logger.warning(
            "WARNING: %d positions still open! Manage them manually.",
            len(executor.positions),
        )
    logger.info("Polybot stopped.")


if __name__ == "__main__":
    main()
