"""
Real-time WebSocket price feed from the Polymarket CLOB.
Streams orderbook changes instantly instead of polling.
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Set, Callable, Optional

import websockets

logger = logging.getLogger("polybot.ws")

WS_URI = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class LivePrice:
    token_id: str
    best_bid: float
    best_ask: float
    mid: float
    last_update: float
    # Momentum tracking
    price_history: list = None  # list of (timestamp, mid) tuples
    momentum: float = 0.0      # positive = rising, negative = falling

    def __post_init__(self):
        if self.price_history is None:
            self.price_history = []


class PriceFeed:
    """
    Maintains a real-time price cache via WebSocket.
    Subscribe to token_ids and get instant price updates.
    """

    def __init__(self):
        self.prices: Dict[str, LivePrice] = {}
        self._subscribed: Set[str] = set()
        self._ws = None
        self._loop = None
        self._thread = None
        self._running = False
        self._on_price_change: Optional[Callable] = None
        self._lock = threading.Lock()

    def start(self):
        """Start the WebSocket feed in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("WebSocket price feed started")

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def subscribe(self, token_ids: list):
        """Add token_ids to the subscription list."""
        new_ids = [t for t in token_ids if t not in self._subscribed]
        if not new_ids:
            return
        with self._lock:
            self._subscribed.update(new_ids)
        # If WS is running, send subscription
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(
                self._send_subscribe(new_ids), self._loop
            )
        logger.info("Subscribed to %d new tokens (total: %d)", len(new_ids), len(self._subscribed))

    def unsubscribe_all(self):
        """Clear all subscriptions and cached prices."""
        with self._lock:
            self._subscribed.clear()
            self.prices.clear()
        logger.info("Unsubscribed from all tokens")

    def get_price(self, token_id: str) -> Optional[LivePrice]:
        """Get the latest price for a token."""
        return self.prices.get(token_id)

    def get_all_prices(self) -> Dict[str, LivePrice]:
        """Get all cached prices."""
        return dict(self.prices)

    def on_price_change(self, callback: Callable):
        """Register a callback for price changes: callback(token_id, LivePrice)"""
        self._on_price_change = callback

    # ── Internal ──────────────────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())

    async def _connect_loop(self):
        """Reconnect loop — auto-reconnects on disconnect."""
        while self._running:
            try:
                async with websockets.connect(
                    WS_URI, ping_interval=20, ping_timeout=10,
                    close_timeout=5, max_size=2**20,
                ) as ws:
                    self._ws = ws
                    logger.info("WebSocket connected to %s", WS_URI)

                    # Re-subscribe all tokens
                    if self._subscribed:
                        await self._send_subscribe(list(self._subscribed))

                    # Read messages
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            self._process_message(raw)
                        except Exception as e:
                            logger.debug("Message parse error: %s", e)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket disconnected, reconnecting in 2s...")
            except Exception as e:
                logger.error("WebSocket error: %s, reconnecting in 5s...", e)
                await asyncio.sleep(5)

            self._ws = None
            if self._running:
                await asyncio.sleep(2)

    async def _send_subscribe(self, token_ids: list):
        """Send subscription message for a batch of tokens."""
        if not self._ws:
            return
        # Polymarket WS accepts assets_ids in subscription
        msg = {
            "auth": {},
            "markets": [],
            "assets_ids": token_ids,
            "type": "market",
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as e:
            logger.error("Subscribe send failed: %s", e)

    def _process_message(self, raw: str):
        """Process incoming WS message and update price cache."""
        data = json.loads(raw)

        # Initial orderbook snapshot
        if "bids" in data and "asks" in data:
            token_id = data.get("asset_id", "")
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                best_bid = max(float(b["price"]) for b in bids)
                best_ask = min(float(a["price"]) for a in asks)
                self._update_price(token_id, best_bid, best_ask)
            return

        # Price change updates
        changes = data.get("price_changes", [])
        for change in changes:
            token_id = change.get("asset_id", "")
            best_bid = float(change.get("best_bid_price", 0) or 0)
            best_ask = float(change.get("best_ask_price", 0) or 0)
            if token_id and (best_bid or best_ask):
                # Update with whatever we have
                existing = self.prices.get(token_id)
                if existing:
                    if best_bid:
                        existing.best_bid = best_bid
                    if best_ask:
                        existing.best_ask = best_ask
                    existing.mid = (existing.best_bid + existing.best_ask) / 2
                    existing.last_update = time.time()
                    if self._on_price_change:
                        self._on_price_change(token_id, existing)
                elif best_bid and best_ask:
                    self._update_price(token_id, best_bid, best_ask)

    def _update_price(self, token_id: str, best_bid: float, best_ask: float):
        now = time.time()
        mid = (best_bid + best_ask) / 2
        existing = self.prices.get(token_id)

        if existing:
            # Keep history (last 60 data points, ~5 minutes at ~5s intervals)
            existing.price_history.append((now, mid))
            if len(existing.price_history) > 60:
                existing.price_history = existing.price_history[-60:]
            existing.best_bid = best_bid
            existing.best_ask = best_ask
            existing.mid = mid
            existing.last_update = now
            # Calculate momentum: price change over last 30s
            existing.momentum = self._calc_momentum(existing.price_history)
            if self._on_price_change:
                self._on_price_change(token_id, existing)
        else:
            lp = LivePrice(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                mid=mid,
                last_update=now,
                price_history=[(now, mid)],
                momentum=0.0,
            )
            self.prices[token_id] = lp
            if self._on_price_change:
                self._on_price_change(token_id, lp)

    @staticmethod
    def _calc_momentum(history: list) -> float:
        """Calculate momentum: price change per minute over last 30s."""
        if len(history) < 2:
            return 0.0
        now = history[-1][0]
        # Find price ~30s ago
        target_time = now - 30
        old_price = history[0][1]
        for ts, price in history:
            if ts >= target_time:
                old_price = price
                break
        current = history[-1][1]
        elapsed = now - max(target_time, history[0][0])
        if elapsed <= 0:
            return 0.0
        # Return change in cents per minute
        return ((current - old_price) * 100) / (elapsed / 60)
