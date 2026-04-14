"""
Orderbook module — fetches real bid/ask from the Polymarket CLOB API
and provides realistic fill simulation for paper trading.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config import CLOB_API_URL

logger = logging.getLogger("polybot.orderbook")


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel]  # sorted high→low
    asks: list[BookLevel]  # sorted low→high
    best_bid: float
    best_ask: float
    spread: float
    bid_depth: float  # total USD on bid side
    ask_depth: float  # total USD on ask side

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2 if self.best_bid and self.best_ask else 0


def fetch_orderbook(token_id: str) -> Optional[OrderBook]:
    """Fetch the full orderbook for a token from the CLOB API."""
    try:
        resp = httpx.get(
            f"{CLOB_API_URL}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Orderbook fetch failed for %s: %s", token_id[:20], exc)
        return None

    raw_bids = data.get("bids", [])
    raw_asks = data.get("asks", [])

    bids = sorted(
        [BookLevel(float(b["price"]), float(b["size"])) for b in raw_bids],
        key=lambda x: -x.price,
    )
    asks = sorted(
        [BookLevel(float(a["price"]), float(a["size"])) for a in raw_asks],
        key=lambda x: x.price,
    )

    best_bid = bids[0].price if bids else 0
    best_ask = asks[0].price if asks else 0
    spread = best_ask - best_bid if best_bid and best_ask else 0

    bid_depth = sum(b.price * b.size for b in bids)
    ask_depth = sum(a.price * a.size for a in asks)

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


def simulate_buy_fill(book: OrderBook, usd_amount: float) -> tuple[float, float]:
    """
    Simulate buying into the ask side of the book.
    Returns (avg_fill_price, total_shares).
    Walks the ask levels consuming liquidity.
    """
    remaining_usd = usd_amount
    total_shares = 0.0

    for level in book.asks:
        if remaining_usd <= 0:
            break
        max_shares_at_level = level.size
        cost_per_share = level.price
        affordable_shares = remaining_usd / cost_per_share
        fill_shares = min(affordable_shares, max_shares_at_level)

        total_shares += fill_shares
        remaining_usd -= fill_shares * cost_per_share

    if total_shares == 0:
        return 0, 0

    avg_price = (usd_amount - remaining_usd) / total_shares
    return avg_price, total_shares


def simulate_sell_fill(book: OrderBook, shares: float) -> tuple[float, float]:
    """
    Simulate selling into the bid side of the book.
    Returns (avg_fill_price, total_usd_received).
    Walks the bid levels consuming liquidity.
    """
    remaining_shares = shares
    total_usd = 0.0

    for level in book.bids:
        if remaining_shares <= 0:
            break
        fill_shares = min(remaining_shares, level.size)
        total_usd += fill_shares * level.price
        remaining_shares -= fill_shares

    filled_shares = shares - remaining_shares
    if filled_shares == 0:
        return 0, 0

    avg_price = total_usd / filled_shares
    return avg_price, total_usd


def analyze_order_flow(book: OrderBook) -> dict:
    """
    Analyze orderbook for signals:
    - bid/ask imbalance: more bids = buying pressure = price likely to rise
    - wall detection: large orders that block price movement
    - depth ratio: which side has more liquidity
    """
    if not book.bids or not book.asks:
        return {"signal": "neutral", "score": 0, "details": "empty book"}

    # 1. Bid/Ask depth imbalance
    #    ratio > 1 = more buying pressure, < 1 = more selling pressure
    depth_ratio = book.bid_depth / max(book.ask_depth, 1)

    # 2. Top-of-book imbalance (first 5 levels)
    top_bid_vol = sum(b.price * b.size for b in book.bids[:5])
    top_ask_vol = sum(a.price * a.size for a in book.asks[:5])
    top_ratio = top_bid_vol / max(top_ask_vol, 1)

    # 3. Wall detection (order > 5x average)
    all_sizes = [b.size for b in book.bids] + [a.size for a in book.asks]
    avg_size = sum(all_sizes) / max(len(all_sizes), 1)

    bid_walls = [b for b in book.bids[:10] if b.size > avg_size * 5]
    ask_walls = [a for a in book.asks[:10] if a.size > avg_size * 5]

    # 4. Spread quality
    spread_cents = book.spread * 100
    tight_spread = spread_cents <= 2.0

    # Score: -100 (bearish) to +100 (bullish)
    score = 0

    # Depth imbalance contributes ±30
    if depth_ratio > 1.5:
        score += 30
    elif depth_ratio > 1.2:
        score += 15
    elif depth_ratio < 0.67:
        score -= 30
    elif depth_ratio < 0.83:
        score -= 15

    # Top-of-book imbalance contributes ±30
    if top_ratio > 2.0:
        score += 30
    elif top_ratio > 1.3:
        score += 15
    elif top_ratio < 0.5:
        score -= 30
    elif top_ratio < 0.77:
        score -= 15

    # Walls contribute ±20
    if bid_walls and not ask_walls:
        score += 20  # bid support = bullish
    elif ask_walls and not bid_walls:
        score -= 20  # ask resistance = bearish

    # Tight spread = healthy market
    if tight_spread:
        score += 10

    # Signal classification
    if score >= 30:
        signal = "bullish"
    elif score <= -30:
        signal = "bearish"
    else:
        signal = "neutral"

    return {
        "signal": signal,
        "score": score,
        "depth_ratio": round(depth_ratio, 2),
        "top_ratio": round(top_ratio, 2),
        "bid_walls": len(bid_walls),
        "ask_walls": len(ask_walls),
        "spread_cents": round(spread_cents, 1),
        "details": (
            f"depth={depth_ratio:.1f}x top={top_ratio:.1f}x "
            f"walls=bid:{len(bid_walls)}/ask:{len(ask_walls)} "
            f"spread={spread_cents:.1f}¢"
        ),
    }


def check_liquidity(book: OrderBook, usd_amount: float, side: str = "BUY") -> dict:
    """
    Check if there's enough liquidity to fill an order.
    Returns a dict with fill quality metrics.
    """
    if side == "BUY":
        avg_price, shares = simulate_buy_fill(book, usd_amount)
        slippage = (avg_price - book.best_ask) / book.best_ask if book.best_ask else 0
    else:
        shares_to_sell = usd_amount / book.best_bid if book.best_bid else 0
        avg_price, total_usd = simulate_sell_fill(book, shares_to_sell)
        slippage = (book.best_bid - avg_price) / book.best_bid if book.best_bid else 0

    return {
        "fillable": avg_price > 0,
        "avg_price": avg_price,
        "slippage_pct": round(slippage * 100, 2),
        "spread": round(book.spread * 100, 2),
        "bid_depth_usd": round(book.bid_depth, 0),
        "ask_depth_usd": round(book.ask_depth, 0),
    }
