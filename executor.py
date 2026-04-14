"""
Order executor for the Underdog Bounce strategy.
Handles position entry (fragmented orders), take-profit, and stop-loss.
"""

import json
import time
import logging
from dataclasses import dataclass, field

import httpx

from auth import ClobAuth
from scanner import Candidate
from config import (
    CLOB_API_URL,
    BOUNCE_TARGET,
    STOP_LOSS,
    ORDER_FRAGMENTS,
    MAX_POSITION_USD,
    MAX_OPEN_POSITIONS,
)

logger = logging.getLogger("polybot.executor")


@dataclass
class Position:
    """Tracks an open underdog position."""

    candidate: Candidate
    entry_price: float
    total_size: float
    filled_fragments: int = 0
    order_ids: list[str] = field(default_factory=list)
    sell_order_ids: list[str] = field(default_factory=list)
    status: str = "OPEN"  # OPEN, CLOSING, CLOSED

    @property
    def take_profit_price(self) -> float:
        return self.entry_price + BOUNCE_TARGET

    @property
    def stop_loss_price(self) -> float:
        return max(self.entry_price - STOP_LOSS, 0.01)


class Executor:
    """Manages order placement and position lifecycle."""

    def __init__(self, auth: ClobAuth):
        self.auth = auth
        self.positions: dict[str, Position] = {}  # token_id → Position
        self.client = httpx.Client(timeout=15)

    # ── Order placement ───────────────────────────────────────────────

    def _post_order(self, payload: dict) -> dict | None:
        """Submit a signed order to the CLOB."""
        body = json.dumps(payload)
        headers = self.auth.l2_headers("POST", "/order", body)
        headers["Content-Type"] = "application/json"
        try:
            resp = self.client.post(
                f"{CLOB_API_URL}/order", content=body, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Order placed: %s", data.get("orderID", data))
            return data
        except httpx.HTTPError as exc:
            logger.error("Order failed: %s", exc)
            return None

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        body = json.dumps({"orderID": order_id})
        headers = self.auth.l2_headers("DELETE", "/order", body)
        headers["Content-Type"] = "application/json"
        try:
            resp = self.client.request(
                "DELETE",
                f"{CLOB_API_URL}/order",
                content=body,
                headers=headers,
            )
            resp.raise_for_status()
            logger.info("Order cancelled: %s", order_id)
            return True
        except httpx.HTTPError as exc:
            logger.error("Cancel failed for %s: %s", order_id, exc)
            return False

    # ── Entry: fragmented buy orders ──────────────────────────────────

    def open_position(self, candidate: Candidate) -> Position | None:
        """
        Place fragmented BUY orders for a candidate underdog.
        Splits MAX_POSITION_USD into ORDER_FRAGMENTS smaller orders,
        spread across a tight price range around the current price.
        """
        if candidate.token_id in self.positions:
            logger.debug("Already have position on %s", candidate.token_id)
            return None

        if len(self.positions) >= MAX_OPEN_POSITIONS:
            logger.warning("Max open positions reached (%d)", MAX_OPEN_POSITIONS)
            return None

        total_usd = MAX_POSITION_USD
        fragment_usd = total_usd / ORDER_FRAGMENTS
        base_price = candidate.price

        # Spread fragments across price ±0.005 around current price
        spread = 0.005
        step = (2 * spread) / max(ORDER_FRAGMENTS - 1, 1)

        order_ids: list[str] = []
        for i in range(ORDER_FRAGMENTS):
            frag_price = round(base_price - spread + step * i, 4)
            frag_price = max(frag_price, 0.01)
            frag_size = fragment_usd / frag_price  # shares

            payload = self.auth.build_order_payload(
                token_id=candidate.token_id,
                price=frag_price,
                size=frag_size,
                side="BUY",
            )
            result = self._post_order(payload)
            if result and result.get("orderID"):
                order_ids.append(result["orderID"])

            time.sleep(0.15)  # small delay between fragments

        if not order_ids:
            logger.error("No fragments filled for %s", candidate.question)
            return None

        position = Position(
            candidate=candidate,
            entry_price=base_price,
            total_size=total_usd / base_price,
            filled_fragments=len(order_ids),
            order_ids=order_ids,
        )
        self.positions[candidate.token_id] = position
        logger.info(
            "OPENED position: %s @ %.2f¢ (%d/%d fragments) | TP=%.2f¢ SL=%.2f¢",
            candidate.question[:60],
            base_price * 100,
            len(order_ids),
            ORDER_FRAGMENTS,
            position.take_profit_price * 100,
            position.stop_loss_price * 100,
        )
        return position

    # ── Exit: take-profit and stop-loss ───────────────────────────────

    def check_exits(self) -> None:
        """
        Check all open positions against current prices.
        Place SELL orders when take-profit or stop-loss triggers.
        """
        for token_id, pos in list(self.positions.items()):
            if pos.status != "OPEN":
                continue

            current_price = self._get_current_price(token_id)
            if current_price is None:
                continue

            should_exit = False
            reason = ""

            if current_price >= pos.take_profit_price:
                should_exit = True
                reason = "TAKE_PROFIT"
            elif current_price <= pos.stop_loss_price:
                should_exit = True
                reason = "STOP_LOSS"

            if should_exit:
                logger.info(
                    "%s triggered for %s: entry=%.2f¢ current=%.2f¢",
                    reason,
                    pos.candidate.question[:50],
                    pos.entry_price * 100,
                    current_price * 100,
                )
                self._close_position(pos, current_price, reason)

    def _get_current_price(self, token_id: str) -> float | None:
        """Fetch the current best price for a token."""
        try:
            resp = self.client.get(
                f"{CLOB_API_URL}/price",
                params={"token_id": token_id, "side": "SELL"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.error("Price fetch failed for %s: %s", token_id, exc)
            return None

    def _close_position(
        self, pos: Position, current_price: float, reason: str
    ) -> None:
        """Place fragmented SELL orders to close a position."""
        pos.status = "CLOSING"

        # Cancel any remaining unfilled buy orders
        for oid in pos.order_ids:
            self._cancel_order(oid)

        # Fragment the sell side too
        frag_size = pos.total_size / ORDER_FRAGMENTS
        sell_ids: list[str] = []

        for _ in range(ORDER_FRAGMENTS):
            payload = self.auth.build_order_payload(
                token_id=pos.candidate.token_id,
                price=current_price,
                size=frag_size,
                side="SELL",
            )
            result = self._post_order(payload)
            if result and result.get("orderID"):
                sell_ids.append(result["orderID"])
            time.sleep(0.1)

        pos.sell_order_ids = sell_ids
        pos.status = "CLOSED"
        pnl_cents = (current_price - pos.entry_price) * 100

        logger.info(
            "CLOSED %s [%s]: entry=%.2f¢ exit=%.2f¢ PnL=%+.2f¢/share | %d sell fragments",
            pos.candidate.question[:50],
            reason,
            pos.entry_price * 100,
            current_price * 100,
            pnl_cents,
            len(sell_ids),
        )

        # Remove from active tracking
        self.positions.pop(pos.candidate.token_id, None)

    # ── Status ────────────────────────────────────────────────────────

    def status_summary(self) -> str:
        """Return a human-readable summary of open positions."""
        if not self.positions:
            return "No open positions"
        lines = []
        for tid, pos in self.positions.items():
            lines.append(
                f"  {pos.candidate.outcome} @ {pos.entry_price*100:.1f}¢ "
                f"(TP {pos.take_profit_price*100:.1f}¢ / SL {pos.stop_loss_price*100:.1f}¢) "
                f"[{pos.filled_fragments}/{ORDER_FRAGMENTS} frags]"
            )
        return f"Open positions ({len(self.positions)}):\n" + "\n".join(lines)
