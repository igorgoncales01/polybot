"""
Paper trading engine — uses real Polymarket prices, fake balance.
Tracks all entries/exits with timestamps for strategy validation.
"""

import json
import time
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

from scanner import scan_markets, Candidate
from orderbook import fetch_orderbook, simulate_buy_fill, simulate_sell_fill, check_liquidity, analyze_order_flow
from ws_feed import PriceFeed
from edge_detector import check_edge, EdgeSignal
from config import (
    BOUNCE_TARGET,
    STOP_LOSS,
    ORDER_FRAGMENTS,
    MAX_POSITION_USD,
    MAX_OPEN_POSITIONS,
    MIN_PRICE,
    MAX_PRICE,
)

# Minimum requirements for entry (relaxed for live markets)
MIN_BID_DEPTH_USD = 200   # at least $200 on bid side
MAX_SPREAD_CENTS = 5.0    # max 5¢ spread
MAX_SLIPPAGE_PCT = 8.0    # max 8% slippage on simulated fill
MAX_ENTRIES_PER_MARKET = 2 # max reentries on same market/side
TRAILING_TRIGGER = 0.03    # when price rises +3¢, move SL to breakeven

logger = logging.getLogger("polybot.paper")

TRADES_LOG = Path(__file__).parent / "paper_trades.json"


@dataclass
class PaperPosition:
    token_id: str
    question: str
    outcome: str
    entry_price: float
    size_usd: float
    shares: float
    entry_time: float
    category: str
    tp_price: float = 0.0
    sl_price: float = 0.0
    high_water: float = 0.0  # highest price seen (for trailing stop)
    trailing_active: bool = False

    def __post_init__(self):
        self.tp_price = self.entry_price + BOUNCE_TARGET
        self.sl_price = max(self.entry_price - STOP_LOSS, 0.01)
        self.high_water = self.entry_price


@dataclass
class PaperTrade:
    token_id: str
    question: str
    outcome: str
    category: str
    action: str  # BUY or SELL
    entry_price: float
    exit_price: float
    size_usd: float
    shares: float
    pnl: float
    reason: str  # ENTRY, TAKE_PROFIT, STOP_LOSS
    timestamp: float
    hold_seconds: float = 0


class PaperTrader:
    """Simulates the Underdog Bounce strategy with real prices."""

    def __init__(self, starting_balance: float = 1000.0):
        self.balance = starting_balance
        self.starting_balance = starting_balance
        self.positions: dict[str, PaperPosition] = {}
        self.trades: list[PaperTrade] = []
        self.scan_count = 0
        self.entry_counts: dict[str, int] = {}  # question -> count of entries
        self.feed = PriceFeed()
        self.feed.start()
        self._load_trades()
        # Rebuild entry counts from trade history
        for t in self.trades:
            if t.action == "BUY":
                key = t.question[:50]
                self.entry_counts[key] = self.entry_counts.get(key, 0) + 1
        # Re-subscribe existing positions
        if self.positions:
            self.feed.subscribe(list(self.positions.keys()))

    # ── Core loop (called by server every scan interval) ──────────

    def tick(self) -> dict:
        """Run one cycle: check exits via WS prices, scan for new entries via Gamma."""
        self.scan_count += 1
        events = []

        # 1. Check exits using REAL-TIME WebSocket prices (instant, no HTTP)
        for tid, pos in list(self.positions.items()):
            ws_price = self.feed.get_price(tid)
            current_price = ws_price.mid if ws_price else 0
            if not current_price:
                continue  # no WS data yet, skip

            # ── TRAILING STOP: update high water mark ──
            if current_price > pos.high_water:
                pos.high_water = current_price
            # Activate trailing when price rises +3¢ above entry
            if not pos.trailing_active and pos.high_water >= pos.entry_price + TRAILING_TRIGGER:
                pos.trailing_active = True
                pos.sl_price = pos.entry_price  # move SL to breakeven
                logger.info(
                    "TRAILING ACTIVE %s: high=%.1f¢, SL moved to breakeven %.1f¢",
                    pos.outcome, pos.high_water * 100, pos.sl_price * 100,
                )
            # Once trailing, keep ratcheting SL up
            if pos.trailing_active:
                new_sl = pos.high_water - TRAILING_TRIGGER  # SL trails 3¢ below high
                if new_sl > pos.sl_price:
                    pos.sl_price = new_sl

            reason = None

            if current_price >= pos.tp_price:
                reason = "TAKE_PROFIT"
            elif current_price <= pos.sl_price:
                reason = "TRAILING_STOP" if pos.trailing_active else "STOP_LOSS"

            if reason:
                # Only fetch orderbook when actually exiting
                book = fetch_orderbook(tid)
                if book:
                    sell_price, sell_usd = simulate_sell_fill(book, pos.shares)
                    exit_price = sell_price if sell_price > 0 else current_price
                    actual_usd = sell_usd if sell_usd > 0 else pos.shares * current_price
                else:
                    exit_price = current_price
                    actual_usd = pos.shares * current_price

                pnl = actual_usd - pos.size_usd
                self.balance += pos.size_usd + pnl
                hold = time.time() - pos.entry_time

                trade = PaperTrade(
                    token_id=tid,
                    question=pos.question,
                    outcome=pos.outcome,
                    category=pos.category,
                    action="SELL",
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    size_usd=pos.size_usd,
                    shares=pos.shares,
                    pnl=round(pnl, 4),
                    reason=reason,
                    timestamp=time.time(),
                    hold_seconds=round(hold, 1),
                )
                self.trades.append(trade)
                del self.positions[tid]

                spread_info = f"spread={book.spread*100:.1f}¢" if book else "no-book"
                events.append({
                    "type": "SELL",
                    "reason": reason,
                    "question": pos.question[:50],
                    "outcome": pos.outcome,
                    "entry": round(pos.entry_price * 100, 1),
                    "exit": round(exit_price * 100, 1),
                    "pnl": round(pnl, 2),
                    "hold": f"{hold:.0f}s",
                    "spread": spread_info,
                })
                logger.info(
                    "PAPER %s %s: %s @ %.1f¢→%.1f¢ PnL=$%.2f (%s) [%s]",
                    reason, pos.outcome, pos.question[:40],
                    pos.entry_price * 100, exit_price * 100, pnl,
                    f"{hold:.0f}s", spread_info,
                )

        # 2b. Scan for new candidates (Gamma API — slower but finds new markets)
        candidates = scan_markets()
        price_map = {c.token_id: c for c in candidates}

        # 3. Open new positions (with edge detection + orderbook analysis)
        # Build set of condition_ids we already have positions in (prevent 2-side bug)
        owned_conditions = set()
        for tid, pos in self.positions.items():
            # Extract condition_id from candidates if available
            for c in candidates:
                if c.token_id == tid:
                    owned_conditions.add(c.condition_id)
                    break

        if len(self.positions) < MAX_OPEN_POSITIONS:
            eligible = [
                c for c in candidates
                if c.token_id not in self.positions
                and c.condition_id not in owned_conditions  # FIX: don't buy both sides
                and MIN_PRICE <= c.price <= MAX_PRICE
                and self.entry_counts.get(c.question[:50], 0) < MAX_ENTRIES_PER_MARKET  # limit reentries
            ]
            eligible.sort(key=lambda c: c.price)

            for cand in eligible:
                if len(self.positions) >= MAX_OPEN_POSITIONS:
                    break
                if self.balance < MAX_POSITION_USD:
                    break

                # ── EDGE CHECK: only enter if external sources confirm value ──
                edge_signal = check_edge(
                    outcome=cand.outcome,
                    question=cand.question,
                    category=cand.category,
                    poly_price=cand.price,
                )

                if edge_signal:
                    if not edge_signal.has_edge:
                        logger.info(
                            "SKIP %s: no edge (poly=%.1f¢ fair=%.1f¢ edge=%.1f¢ src=%s)",
                            cand.outcome, cand.price * 100,
                            edge_signal.fair_price * 100,
                            edge_signal.edge_cents,
                            edge_signal.source,
                        )
                        continue
                    logger.info(
                        "✅ EDGE FOUND %s: poly=%.1f¢ fair=%.1f¢ edge=+%.1f¢ [%s] %s",
                        cand.outcome, cand.price * 100,
                        edge_signal.fair_price * 100,
                        edge_signal.edge_cents,
                        edge_signal.source,
                        edge_signal.details,
                    )
                else:
                    # No external data — skip (don't buy blind)
                    logger.debug("SKIP %s: no external price data", cand.outcome)
                    continue

                # Fetch real orderbook before entering
                book = fetch_orderbook(cand.token_id)
                if not book:
                    logger.debug("SKIP %s: no orderbook", cand.outcome)
                    continue

                # Check spread, depth, and slippage
                if book.spread * 100 > MAX_SPREAD_CENTS:
                    logger.info(
                        "SKIP %s: spread %.1f¢ > %.1f¢ max",
                        cand.outcome, book.spread * 100, MAX_SPREAD_CENTS,
                    )
                    continue

                if book.bid_depth < MIN_BID_DEPTH_USD:
                    logger.info(
                        "SKIP %s: bid depth $%.0f < $%.0f min",
                        cand.outcome, book.bid_depth, MIN_BID_DEPTH_USD,
                    )
                    continue

                # Simulate buying into the ask side
                size_usd = MAX_POSITION_USD
                avg_fill, shares = simulate_buy_fill(book, size_usd)
                if avg_fill == 0 or shares == 0:
                    logger.info("SKIP %s: no ask liquidity", cand.outcome)
                    continue

                slippage_pct = (avg_fill - book.best_ask) / book.best_ask * 100
                if slippage_pct > MAX_SLIPPAGE_PCT:
                    logger.info(
                        "SKIP %s: slippage %.1f%% > %.1f%% max",
                        cand.outcome, slippage_pct, MAX_SLIPPAGE_PCT,
                    )
                    continue

                # ── ORDER FLOW CHECK: skip if book is bearish (unless edge is huge) ──
                flow = analyze_order_flow(book)
                if flow["signal"] == "bearish" and edge_signal.edge_cents < 15:
                    logger.info(
                        "SKIP %s: bearish order flow (score=%d %s)",
                        cand.outcome, flow["score"], flow["details"],
                    )
                    continue
                if flow["signal"] == "bearish" and edge_signal.edge_cents >= 15:
                    logger.info(
                        "⚡ OVERRIDE %s: bearish flow BUT edge=+%.1f¢ > 15¢ — entering anyway",
                        cand.outcome, edge_signal.edge_cents,
                    )

                # ── MOMENTUM CHECK: skip if price is falling fast ──
                ws_price = self.feed.get_price(cand.token_id)
                momentum = ws_price.momentum if ws_price else 0
                if momentum < -2.0:  # falling more than 2¢/min
                    logger.info(
                        "SKIP %s: negative momentum %.1f¢/min (price falling)",
                        cand.outcome, momentum,
                    )
                    continue

                # ── DYNAMIC TP: scale with edge size ──
                dynamic_tp = max(BOUNCE_TARGET, edge_signal.edge_cents / 100 / 3)

                # ── DYNAMIC POSITION SIZE: bigger edge = bigger bet ──
                if edge_signal.edge_cents >= 15:
                    size_usd = min(MAX_POSITION_USD * 2, self.balance * 0.03)  # up to 2x
                elif edge_signal.edge_cents >= 10:
                    size_usd = MAX_POSITION_USD * 1.5
                else:
                    size_usd = MAX_POSITION_USD
                size_usd = round(min(size_usd, self.balance - 100), 2)  # keep $100 reserve
                if size_usd < 10:
                    continue

                # Recalculate fill with new size
                avg_fill, shares = simulate_buy_fill(book, size_usd)
                if avg_fill == 0 or shares == 0:
                    continue

                entry_price = avg_fill
                pos = PaperPosition(
                    token_id=cand.token_id,
                    question=cand.question,
                    outcome=cand.outcome,
                    entry_price=entry_price,
                    size_usd=size_usd,
                    shares=round(shares, 2),
                    entry_time=time.time(),
                    category=cand.category,
                )
                # Apply dynamic TP
                pos.tp_price = entry_price + dynamic_tp
                self.positions[cand.token_id] = pos
                self.balance -= size_usd
                # Track condition to prevent buying other side
                owned_conditions.add(cand.condition_id)
                # Track entry count to limit reentries
                mkt_key = cand.question[:50]
                self.entry_counts[mkt_key] = self.entry_counts.get(mkt_key, 0) + 1
                # Subscribe to real-time WS feed for this token
                self.feed.subscribe([cand.token_id])

                trade = PaperTrade(
                    token_id=cand.token_id,
                    question=cand.question,
                    outcome=cand.outcome,
                    category=cand.category,
                    action="BUY",
                    entry_price=entry_price,
                    exit_price=0,
                    size_usd=size_usd,
                    shares=round(shares, 2),
                    pnl=0,
                    reason=f"ENTRY|edge={edge_signal.edge_cents:+.1f}¢|{edge_signal.source}",
                    timestamp=time.time(),
                )
                self.trades.append(trade)

                events.append({
                    "type": "BUY",
                    "reason": "ENTRY",
                    "question": cand.question[:50],
                    "outcome": cand.outcome,
                    "price": round(entry_price * 100, 1),
                    "mid": round(cand.price * 100, 1),
                    "size": size_usd,
                    "shares": round(shares, 1),
                    "tp": round(pos.tp_price * 100, 1),
                    "sl": round(pos.sl_price * 100, 1),
                    "spread": round(book.spread * 100, 1),
                    "slippage": round(slippage_pct, 2),
                    "bid_depth": round(book.bid_depth, 0),
                    "edge": edge_signal.edge_cents,
                    "edge_source": edge_signal.source,
                    "fair_price": round(edge_signal.fair_price * 100, 1),
                    "flow_signal": flow["signal"],
                    "flow_score": flow["score"],
                    "momentum": round(momentum, 1),
                })
                logger.info(
                    "PAPER BUY %s @ %.1f¢ (fair=%.1f¢ edge=+%.1f¢ %s) "
                    "$%.0f → %.0f shares | TP=%.1f¢ SL=%.1f¢ | "
                    "flow=%s(%d) mom=%.1f¢/min",
                    cand.outcome, entry_price * 100,
                    edge_signal.fair_price * 100, edge_signal.edge_cents, edge_signal.source,
                    size_usd, shares, pos.tp_price * 100, pos.sl_price * 100,
                    flow["signal"], flow["score"], momentum,
                )

        self._save_trades()
        return self._snapshot(events, price_map)

    # ── Snapshot for the dashboard ────────────────────────────────

    def _snapshot(self, events: list, price_map: dict) -> dict:
        sells = [t for t in self.trades if t.action == "SELL"]
        wins = [t for t in sells if t.pnl > 0]
        losses = [t for t in sells if t.pnl <= 0]
        total_pnl = sum(t.pnl for t in sells)
        open_pnl = 0
        open_positions = []

        for tid, pos in self.positions.items():
            ws = self.feed.get_price(tid)
            live = price_map.get(tid)
            # Prefer WS price (real-time), fallback to Gamma scan
            current = ws.mid if ws else (live.price if live else pos.entry_price)
            unrealized = (current - pos.entry_price) * pos.shares
            open_pnl += unrealized
            open_positions.append({
                "token_id": tid,
                "question": pos.question,
                "outcome": pos.outcome,
                "category": pos.category,
                "entry_price": round(pos.entry_price * 100, 1),
                "current_price": round(current * 100, 1),
                "tp_price": round(pos.tp_price * 100, 1),
                "sl_price": round(pos.sl_price * 100, 1),
                "size_usd": pos.size_usd,
                "shares": pos.shares,
                "unrealized_pnl": round(unrealized, 2),
                "hold_seconds": round(time.time() - pos.entry_time, 0),
            })

        return {
            "scan_count": self.scan_count,
            "balance": round(self.balance, 2),
            "starting_balance": self.starting_balance,
            "total_pnl": round(total_pnl, 2),
            "open_pnl": round(open_pnl, 2),
            "open_positions": open_positions,
            "total_trades": len(sells),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / max(len(sells), 1) * 100, 1),
            "avg_win": round(sum(t.pnl for t in wins) / max(len(wins), 1), 2),
            "avg_loss": round(sum(t.pnl for t in losses) / max(len(losses), 1), 2),
            "events": events,
            "closed_trades": [asdict(t) for t in self.trades if t.action == "SELL"],
            "recent_trades": [asdict(t) for t in self.trades[-20:]],
        }

    # ── Persistence ───────────────────────────────────────────────

    def _save_trades(self):
        try:
            data = {
                "balance": self.balance,
                "initial_balance": self.starting_balance,
                "trades": [asdict(t) for t in self.trades],
                "positions": {
                    tid: {
                        "token_id": p.token_id,
                        "question": p.question,
                        "outcome": p.outcome,
                        "entry_price": p.entry_price,
                        "size_usd": p.size_usd,
                        "shares": p.shares,
                        "entry_time": p.entry_time,
                        "category": p.category,
                    }
                    for tid, p in self.positions.items()
                },
            }
            TRADES_LOG.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error("Failed to save trades: %s", e)

    def _load_trades(self):
        if not TRADES_LOG.exists():
            return
        try:
            data = json.loads(TRADES_LOG.read_text())
            self.balance = data.get("balance", self.starting_balance)
            self.starting_balance = data.get("initial_balance", self.starting_balance)
            for td in data.get("trades", []):
                self.trades.append(PaperTrade(**td))
            for tid, pd in data.get("positions", {}).items():
                self.positions[tid] = PaperPosition(**pd)
            logger.info(
                "Loaded %d trades, %d positions, balance=$%.2f",
                len(self.trades), len(self.positions), self.balance,
            )
        except Exception as e:
            logger.error("Failed to load trades: %s", e)

    def reset(self):
        """Full reset — clear everything and wipe the JSON file."""
        self.balance = self.starting_balance
        self.positions.clear()
        self.trades.clear()
        self.scan_count = 0
        self.feed.unsubscribe_all()
        # Wipe the file
        data = {
            "balance": self.starting_balance,
            "initial_balance": self.starting_balance,
            "trades": [],
            "positions": {},
        }
        TRADES_LOG.write_text(json.dumps(data, indent=2))
        logger.info("Paper trader RESET — balance=$%.2f", self.starting_balance)
