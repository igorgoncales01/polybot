"""
Kelly Criterion — optimal position sizing based on edge and odds.

Given:
  - fair_price: true probability from external sources (e.g. 0.40 = 40%)
  - poly_price: Polymarket price (e.g. 0.25 = 25¢)
  - bankroll: current balance

Returns the optimal bet size in USD.

Uses fractional Kelly (25%) to reduce variance — full Kelly is too aggressive.
"""

import logging

logger = logging.getLogger("polybot.kelly")

KELLY_FRACTION = 0.15  # use 15% Kelly to reduce variance
MIN_BET_USD = 10.0
MAX_BET_PCT = 0.015    # never bet more than 1.5% of bankroll
MAX_BET_USD = 150.0    # absolute cap per trade


def kelly_size(fair_price: float, poly_price: float, bankroll: float) -> float:
    """
    Calculate optimal bet size using Kelly Criterion.

    For a binary outcome at price p with true probability q:
      Kelly fraction f = (q * (1/p - 1) - (1-q)) / (1/p - 1)
      Simplified: f = (q - p) / (1 - p)

    Returns bet size in USD (0 if no edge).
    """
    if fair_price <= poly_price:
        return 0.0  # no edge

    if poly_price <= 0 or poly_price >= 1 or fair_price <= 0 or fair_price >= 1:
        return 0.0

    # Kelly formula for binary bets
    # b = net odds = (1/p - 1) = payout ratio
    # f = (b*q - (1-q)) / b  where q=fair_price, b=1/poly_price - 1
    b = (1.0 / poly_price) - 1.0  # net odds (e.g. price=0.25 → b=3.0 → 3:1)
    q = fair_price                 # true win probability

    f = (b * q - (1.0 - q)) / b

    if f <= 0:
        return 0.0

    # Apply fractional Kelly
    f *= KELLY_FRACTION

    # Cap at MAX_BET_PCT of bankroll
    f = min(f, MAX_BET_PCT)

    bet = round(bankroll * f, 2)
    bet = min(bet, MAX_BET_USD)  # absolute cap
    bet = max(bet, MIN_BET_USD) if bet > MIN_BET_USD * 0.5 else 0.0

    logger.info(
        "KELLY: fair=%.1f%% poly=%.1f%% edge=%.1f%% odds=%.1f:1 "
        "kelly_f=%.3f frac=%.3f bet=$%.2f (%.1f%% of $%.0f)",
        fair_price * 100, poly_price * 100,
        (fair_price - poly_price) * 100, b,
        f / KELLY_FRACTION, f, bet, f * 100, bankroll,
    )

    return bet
