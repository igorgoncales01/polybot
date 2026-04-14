#!/usr/bin/env python3
"""
PolyBot server — serves the dashboard, real-time market data,
and paper trading engine.
"""

import json
import logging
import os
import time
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

from scanner import scan_markets
from paper_trader import PaperTrader
from edge_detector import get_all_live_edges

# ── Shared state ──────────────────────────────────────────────────
_cache = {"candidates": [], "last_scan": 0, "scan_count": 0}
_lock = threading.Lock()
CACHE_TTL = 8

_paper = PaperTrader(starting_balance=10000.0)
_paper_lock = threading.Lock()
_paper_snapshot = {}


def _refresh_if_stale():
    now = time.time()
    with _lock:
        if now - _cache["last_scan"] < CACHE_TTL:
            return
    candidates = scan_markets()
    with _lock:
        _cache["candidates"] = [
            {
                "condition_id": c.condition_id,
                "token_id": c.token_id,
                "question": c.question,
                "outcome": c.outcome,
                "price": c.price,
                "liquidity": c.liquidity,
                "category": c.category,
            }
            for c in candidates
        ]
        _cache["last_scan"] = time.time()
        _cache["scan_count"] += 1


def _paper_tick():
    """Run one paper trading cycle."""
    global _paper_snapshot
    with _paper_lock:
        snapshot = _paper.tick()
        _paper_snapshot = snapshot
    return snapshot


# ── Background paper trading loop ─────────────────────────────────
_paper_running = False
_paper_thread = None


def _paper_loop():
    while _paper_running:
        try:
            _paper_tick()
        except Exception as e:
            logging.getLogger("polybot.server").error("Paper tick error: %s", e)
        time.sleep(CACHE_TTL)


def start_paper():
    global _paper_running, _paper_thread
    if _paper_running:
        return
    _paper_running = True
    _paper_thread = threading.Thread(target=_paper_loop, daemon=True)
    _paper_thread.start()


def stop_paper():
    global _paper_running
    _paper_running = False


def reset_paper():
    global _paper_snapshot
    with _paper_lock:
        _paper.reset()
        _paper_snapshot = {}


# ── HTTP Handler ──────────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/candidates":
            _refresh_if_stale()
            with _lock:
                payload = {
                    "candidates": _cache["candidates"],
                    "scan_count": _cache["scan_count"],
                    "count": len(_cache["candidates"]),
                }
            return self._json_response(payload)

        if path == "/api/paper":
            with _paper_lock:
                snapshot = _paper_snapshot if _paper_snapshot else _paper._snapshot([], {})
            return self._json_response({
                "running": _paper_running,
                **snapshot,
            })

        if path == "/api/paper/start":
            start_paper()
            return self._json_response({"status": "started"})

        if path == "/api/paper/stop":
            stop_paper()
            return self._json_response({"status": "stopped"})

        if path == "/api/paper/reset":
            stop_paper()
            reset_paper()
            return self._json_response({"status": "reset"})

        if path == "/api/edge":
            # Show all candidates with edge analysis
            _refresh_if_stale()
            with _lock:
                cands = _cache["candidates"]
            enriched = get_all_live_edges(cands)
            with_edge = [c for c in enriched if c.get("edge", {}).get("has_edge")]
            return self._json_response({
                "total_candidates": len(enriched),
                "with_edge": len(with_edge),
                "candidates": enriched,
            })

        return super().do_GET()

    def log_message(self, fmt, *args):
        pass


def main():
    port = 8090
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"PolyBot server on http://localhost:{port}")
    print(f"Dashboard: http://localhost:{port}/polybot.html")
    print(f"Paper API: /api/paper | /api/paper/start | /api/paper/stop")
    start_paper()  # auto-start on boot
    server.serve_forever()


if __name__ == "__main__":
    main()
