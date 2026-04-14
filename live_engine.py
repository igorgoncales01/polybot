"""
live_engine.py — Live Soccer Signal Engine (TEST MODULE)

Detects price discrepancies between SportMonks live bookmaker odds
and Polymarket prices. Does NOT execute real trades.

Signals are stored in _signals (last 100) and served via /api/live-engine.

Usage:
  python live_engine.py          # continuous loop (Ctrl+C to stop)
  python live_engine.py --once   # single cycle and print results
"""
from __future__ import annotations

import csv
import logging
import os
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass

import httpx

from sportmonks_client import SMFixture, sm_client

logger = logging.getLogger("polybot.live_engine")

# ── Config ─────────────────────────────────────────────────────────────────
CLOB_API   = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
GAMMA_API  = "https://gamma-api.polymarket.com"
LOOP_INTERVAL = 30  # seconds

# Grade A thresholds
_A_SHIFT = 20.0   # ¢ odds moved since game start
_A_LAG   = 12.0   # ¢ PM behind bookmakers
_A_BOOKS = 5      # min bookmakers in consensus
_A_MIN_LO = 68    # earliest minute to enter
_A_MIN_HI = 82    # latest minute to enter

# Grade B thresholds
_B_SHIFT = 15.0
_B_LAG   = 10.0
_B_BOOKS = 3
_B_MIN_LO = 65
_B_MIN_HI = 85

MAX_SIGNALS = 100
LOG_FILE    = "live_engine_log.csv"

_CSV_FIELDS = [
    "timestamp", "grade", "game", "outcome", "minute", "period", "score",
    "home_xg", "away_xg", "xg_signal", "shift_cents", "lag_cents",
    "live_prob", "pm_price", "n_bookmakers", "would_enter", "tp", "sl",
]

# Soccer tag slugs to scan on Polymarket
_SOCCER_TAGS = [
    "ucl", "epl", "soccer", "la-liga", "bundesliga", "serie-a",
    "ligue-one", "europa-league", "mls", "conference-league",
]


# ── Data ───────────────────────────────────────────────────────────────────
@dataclass
class LiveSignal:
    timestamp: float
    grade: str         # "A" | "B" | "SKIP"
    game: str          # "Barcelona vs PSG"
    outcome: str       # "Barcelona"
    minute: int
    period: str
    score: str         # "1-0"
    home_xg: float
    away_xg: float
    xg_signal: str     # "STRONG" | "WEAK" | "N/A"
    shift_cents: float
    lag_cents: float
    live_prob: float
    pm_price: float
    n_bookmakers: int
    would_enter: bool
    tp: float
    sl: float
    fixture_id: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["time_str"] = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        return d


# ── Module state ───────────────────────────────────────────────────────────
_signals: list[LiveSignal] = []
_lock    = threading.Lock()
_running = False
_cycles  = 0
_last_update = ""
_live_fixture_count = 0  # fixtures found in last cycle
_thread: threading.Thread | None = None


# ── Engine ─────────────────────────────────────────────────────────────────
class LiveEngine:
    def __init__(self) -> None:
        # fixture_id → (baseline_home_prob, baseline_away_prob)
        self._baselines: dict[int, tuple[float, float]] = {}
        # token_id → (ts, price)
        self._price_cache: dict[str, tuple[float, float]] = {}
        # (ts, candidates_list)
        self._pm_cache: tuple[float, list[dict]] = (0.0, [])
        self._csv_ready = False

    # ── Helpers ────────────────────────────────────────────────────────────

    def _init_csv(self) -> None:
        if self._csv_ready:
            return
        if not os.path.exists(LOG_FILE):
            with open(LOG_FILE, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=_CSV_FIELDS).writeheader()
        self._csv_ready = True

    def _write_csv(self, sig: LiveSignal) -> None:
        try:
            self._init_csv()
            row = {k: getattr(sig, k, "") for k in _CSV_FIELDS}
            with open(LOG_FILE, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=_CSV_FIELDS).writerow(row)
        except Exception as exc:
            logger.debug("CSV write: %s", exc)

    def _get_pm_candidates(self) -> list[dict]:
        """Soccer candidates from Gamma /events. Cached 5 min."""
        now = time.time()
        ts, cached = self._pm_cache
        if now - ts < 300 and cached:
            return cached

        candidates: list[dict] = []
        seen: set[str] = set()
        skip_kw = (
            "spread", "handicap", "draw", "both teams", "score first",
            "o/u", "over/under", "1h ", "2h ", "half",
            "exact score", "win the ", "win a trophy", "reach the",
            "appointed", "top scorer", "player of", "qualify",
            "place ", "finish ", "most goals", "most player",
            "europa league winner", "conference league winner",
        )

        for tag in _SOCCER_TAGS:
            try:
                resp = httpx.get(
                    f"{GAMMA_API}/events",
                    params={
                        "active": "true", "closed": "false",
                        "limit": 50, "tag_slug": tag,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                events = resp.json()
            except Exception as exc:
                logger.debug("Gamma tag=%s: %s", tag, exc)
                continue

            for event in events:
                for m in event.get("markets", []):
                    q = (m.get("question") or "").lower()
                    if any(k in q for k in skip_kw):
                        continue
                    cid = m.get("conditionId", "")
                    if cid in seen:
                        continue
                    # Parse clobTokenIds and outcomes (stored as JSON strings or lists)
                    import json as _json
                    raw_ids = m.get("clobTokenIds") or []
                    if isinstance(raw_ids, str):
                        try:
                            raw_ids = _json.loads(raw_ids)
                        except Exception:
                            raw_ids = []
                    raw_outcomes = m.get("outcomes") or []
                    if isinstance(raw_outcomes, str):
                        try:
                            raw_outcomes = _json.loads(raw_outcomes)
                        except Exception:
                            raw_outcomes = []
                    if not raw_ids:
                        continue
                    seen.add(cid)
                    # Parse outcomePrices for direct PM prices
                    raw_prices = m.get("outcomePrices") or []
                    if isinstance(raw_prices, str):
                        try:
                            raw_prices = _json.loads(raw_prices)
                        except Exception:
                            raw_prices = []
                    for i, tid in enumerate(raw_ids):
                        if not tid:
                            continue
                        outcome_label = raw_outcomes[i] if i < len(raw_outcomes) else str(i)
                        pm_price_cached = 0.0
                        try:
                            pm_price_cached = float(raw_prices[i]) if i < len(raw_prices) else 0.0
                        except (TypeError, ValueError):
                            pm_price_cached = 0.0
                        candidates.append({
                            "token_id":  tid,
                            "question":  m.get("question", ""),
                            "outcome":   outcome_label,
                            "category":  tag,
                            "pm_price":  pm_price_cached,  # from Gamma API
                        })

        self._pm_cache = (now, candidates)
        logger.debug("PM soccer candidates: %d", len(candidates))
        return candidates

    def _get_pm_price(self, token_id: str) -> float:
        """Fetch CLOB midpoint. Cached 15s."""
        now = time.time()
        if token_id in self._price_cache:
            ts, price = self._price_cache[token_id]
            if now - ts < 15:
                return price

        try:
            resp = httpx.get(
                f"{CLOB_API}/midpoints",
                params={"token_id": token_id},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            if "mid" in data:
                price = float(data["mid"])
            elif "midpoints" in data:
                price = float((data["midpoints"] or {}).get(token_id, 0) or 0)
            else:
                price = 0.0
        except Exception:
            price = 0.0

        self._price_cache[token_id] = (now, price)
        return price

    @staticmethod
    def _norm(s: str) -> str:
        """Normalize unicode: remove accents, lowercase."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()

    def _team_match(self, team: str, text: str) -> bool:
        """Fuzzy: meaningful parts of team name found in text (accent-insensitive)."""
        t = self._norm(text)
        tl = self._norm(team)
        if tl in t:
            return True
        parts = [p for p in tl.split() if len(p) > 2]
        if parts and all(p in t for p in parts):
            return True
        return False

    def _match_candidates(
        self, fixture: SMFixture, candidates: list[dict]
    ) -> list[tuple[dict, str]]:
        """
        Return (candidate, side='home'|'away') for matches.
        Handles two market formats:
          1. "Will X win on DATE?" → outcome Yes/No (Polymarket UCL style)
          2. "X vs Y" → outcome = team name
        Only returns the YES/winning side.
        """
        results = []
        for c in candidates:
            q = c["question"]
            outcome = (c["outcome"] or "").lower()

            # Format 1: "Will X win on YYYY-MM-DD?" — outcome is Yes/No
            # Require "win on" + date pattern to avoid tournament/other markets
            if outcome == "yes":
                import re as _re
                # Must be "win on YYYY-MM-DD" market
                date_m = _re.search(r"win on (\d{4}-\d{2}-\d{2})", q.lower())
                if not date_m:
                    continue
                # Date must match fixture game_date (if known)
                if fixture.game_date and date_m.group(1) != fixture.game_date:
                    continue
                home_in_q = self._team_match(fixture.home_team, q)
                away_in_q = self._team_match(fixture.away_team, q)
                if home_in_q and not away_in_q:
                    results.append((c, "home"))
                elif away_in_q and not home_in_q:
                    results.append((c, "away"))
                # if both teams in question → ambiguous, skip
            elif outcome == "no":
                continue  # we never want to buy "No" (team loses)
            else:
                # Format 2: "X vs Y" — both teams in question, outcome = team name
                if not (self._team_match(fixture.home_team, q)
                        and self._team_match(fixture.away_team, q)):
                    continue
                if self._team_match(fixture.home_team, outcome):
                    results.append((c, "home"))
                elif self._team_match(fixture.away_team, outcome):
                    results.append((c, "away"))
        return results

    def _xg_signal(self, fixture: SMFixture, side: str) -> str:
        """STRONG: leader has more xG.  WEAK: leader has less xG.  N/A: no data."""
        if fixture.home_xg == 0 and fixture.away_xg == 0:
            return "N/A"
        if fixture.leader == "draw":
            return "N/A"
        our_xg  = fixture.home_xg if side == "home" else fixture.away_xg
        opp_xg  = fixture.away_xg if side == "home" else fixture.home_xg
        if our_xg >= opp_xg + 0.4:
            return "STRONG"
        if opp_xg >= our_xg + 0.4:
            return "WEAK"
        return "N/A"

    def _grade(
        self,
        shift: float,
        lag: float,
        books: int,
        minute: int,
        xg: str,
    ) -> str:
        if (shift >= _A_SHIFT and lag >= _A_LAG
                and books >= _A_BOOKS
                and _A_MIN_LO <= minute <= _A_MIN_HI):
            base = "A"
        elif (shift >= _B_SHIFT and lag >= _B_LAG
              and books >= _B_BOOKS
              and _B_MIN_LO <= minute <= _B_MIN_HI):
            base = "B"
        else:
            base = "SKIP"

        # xG modifier
        if xg == "WEAK":
            base = {"A": "B", "B": "SKIP", "SKIP": "SKIP"}[base]
        elif xg == "STRONG" and base == "B":
            base = "A"

        return base

    # ── Main cycle ─────────────────────────────────────────────────────────

    def run_cycle(self) -> list[LiveSignal]:
        global _cycles, _last_update, _live_fixture_count

        fixtures   = sm_client.get_live_fixtures()
        candidates = self._get_pm_candidates()

        _cycles += 1
        _last_update = time.strftime("%H:%M:%S")
        _live_fixture_count = len(fixtures)

        if not fixtures:
            logger.debug("Cycle %d: no live fixtures from SportMonks", _cycles)
            return []
        if not candidates:
            logger.debug("Cycle %d: no PM soccer candidates", _cycles)
            return []

        logger.debug("Cycle %d: %d live fixtures, %d PM candidates", _cycles, len(fixtures), len(candidates))
        cycle_signals: list[LiveSignal] = []

        for fixture in fixtures:
            # Only 2nd half or ET, min 60+, with a goal
            if fixture.period not in ("2H", "ET"):
                continue
            if fixture.minute < 60:
                continue
            if fixture.home_score == 0 and fixture.away_score == 0:
                continue  # skip 0-0 (nothing to trade)

            prob_h, prob_a, n_books = sm_client.get_live_odds(fixture.fixture_id)
            if n_books == 0:
                continue

            # Baseline: first time we see this fixture → store and skip
            if fixture.fixture_id not in self._baselines:
                self._baselines[fixture.fixture_id] = (prob_h, prob_a)
                logger.debug(
                    "Baseline %s vs %s: H=%.3f A=%.3f",
                    fixture.home_team, fixture.away_team, prob_h, prob_a,
                )
                continue

            base_h, base_a = self._baselines[fixture.fixture_id]

            matches = self._match_candidates(fixture, candidates)
            if not matches:
                logger.debug("No PM match: %s vs %s", fixture.home_team, fixture.away_team)
                continue

            for cand, side in matches:
                live_prob     = prob_h if side == "home" else prob_a
                baseline_prob = base_h if side == "home" else base_a

                if live_prob < 0.50:
                    continue  # only trade the leader

                token_id = cand["token_id"]
                # Use Gamma price (fast), fallback to CLOB
                pm_price = cand.get("pm_price") or self._get_pm_price(token_id)
                if pm_price <= 0.05 or pm_price >= 0.97:
                    continue

                shift = round((live_prob - baseline_prob) * 100, 1)
                lag   = round((live_prob - pm_price) * 100, 1)

                if lag < 5.0:
                    continue  # PM already caught up

                xg_sig = self._xg_signal(fixture, side)
                grade  = self._grade(shift, lag, n_books, fixture.minute, xg_sig)
                would_enter = grade in ("A", "B")

                tp = round(pm_price + (lag / 100) * 0.70, 3)
                sl = round(pm_price - 0.08, 3)

                sig = LiveSignal(
                    timestamp   = time.time(),
                    grade       = grade,
                    game        = f"{fixture.home_team} vs {fixture.away_team}",
                    outcome     = cand["outcome"],
                    minute      = fixture.minute,
                    period      = fixture.period,
                    score       = fixture.score_str,
                    home_xg     = fixture.home_xg,
                    away_xg     = fixture.away_xg,
                    xg_signal   = xg_sig,
                    shift_cents = shift,
                    lag_cents   = lag,
                    live_prob   = live_prob,
                    pm_price    = pm_price,
                    n_bookmakers= n_books,
                    would_enter = would_enter,
                    tp          = tp,
                    sl          = sl,
                    fixture_id  = fixture.fixture_id,
                )

                cycle_signals.append(sig)
                self._write_csv(sig)

                icon = "★★" if grade == "A" else ("★" if grade == "B" else "—")
                logger.info(
                    "[LIVE] %s %s | min %d %s | %s | "
                    "shift %+.1f¢ lag %+.1f¢ books %d xG %s | "
                    "PM %.3f LIVE %.3f | %s",
                    icon, sig.game, fixture.minute, fixture.period,
                    fixture.score_str,
                    shift, lag, n_books, xg_sig,
                    pm_price, live_prob,
                    "WOULD ENTER: " + cand["outcome"] if would_enter else "skip",
                )

        with _lock:
            _signals.extend(cycle_signals)
            if len(_signals) > MAX_SIGNALS:
                del _signals[:-MAX_SIGNALS]

        return cycle_signals


# ── Background loop ────────────────────────────────────────────────────────

def _loop(engine: LiveEngine) -> None:
    logger.info("Live Engine started — interval %ds", LOOP_INTERVAL)
    while _running:
        try:
            sigs = engine.run_cycle()
            if sigs:
                a = sum(1 for s in sigs if s.grade == "A")
                b = sum(1 for s in sigs if s.grade == "B")
                logger.debug("Cycle %d: %d signals (A=%d B=%d)", _cycles, len(sigs), a, b)
        except Exception as exc:
            logger.error("Live Engine error: %s", exc, exc_info=True)
        time.sleep(LOOP_INTERVAL)


_engine = LiveEngine()


def start() -> None:
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(
        target=_loop, args=(_engine,), daemon=True, name="live-engine"
    )
    _thread.start()


def stop() -> None:
    global _running
    _running = False


def get_state() -> dict:
    with _lock:
        sigs = list(_signals)
    a    = sum(1 for s in sigs if s.grade == "A")
    b    = sum(1 for s in sigs if s.grade == "B")
    skip = sum(1 for s in sigs if s.grade == "SKIP")
    return {
        "running":            _running,
        "cycles":             _cycles,
        "last_update":        _last_update,
        "live_fixture_count": _live_fixture_count,
        "total_signals":      len(sigs),
        "grade_a":   a,
        "grade_b":   b,
        "grade_skip": skip,
        "signals": [s.to_dict() for s in reversed(sigs)],  # newest first
    }


# ── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    engine = LiveEngine()

    if "--once" in sys.argv:
        sigs = engine.run_cycle()
        print(f"\n=== Cycle complete: {len(sigs)} signals ===")
        for s in sigs:
            icon = "★★" if s.grade == "A" else ("★" if s.grade == "B" else "—")
            print(
                f"  {icon} {s.grade:4} | {s.game} | min {s.minute} {s.period} "
                f"| {s.score} | lag {s.lag_cents:+.1f}¢ shift {s.shift_cents:+.1f}¢ "
                f"| xG:{s.xg_signal} | {s.outcome}"
            )
        if not sigs:
            print("  (no signals — check logs for details)")
    else:
        start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            stop()
            print("Stopped.")
