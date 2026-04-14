"""SportMonks API v3 client — live soccer fixtures, scores, xG, and in-play odds."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger("polybot.sportmonks")

SPORTMONKS_BASE = "https://api.sportmonks.com/v3/football"
CACHE_TTL = 30  # seconds

# SportMonks type_id values for statistics (xG variants)
_XG_TYPE_IDS = {116, 117, 118}
# SportMonks type_id values for goal events
_GOAL_TYPE_IDS = {14, 15, 16}  # Goal, Own Goal, Penalty Goal


@dataclass
class SMFixture:
    fixture_id: int
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    minute: int        # current match minute, 0 if unknown
    period: str        # "1H" | "2H" | "HT" | "ET" | "FT"
    home_xg: float
    away_xg: float
    last_goal_minute: int  # minute of latest goal, 0 if none

    @property
    def score_str(self) -> str:
        return f"{self.home_score}-{self.away_score}"

    @property
    def leader(self) -> str:
        """Returns 'home', 'away', or 'draw'."""
        if self.home_score > self.away_score:
            return "home"
        if self.away_score > self.home_score:
            return "away"
        return "draw"


class SportMonksClient:
    def __init__(self) -> None:
        self._api_key = os.getenv("SPORTMONKS_API_KEY", "")
        self._fixtures_cache: tuple[float, list[SMFixture]] = (0.0, [])
        # fixture_id → (ts, (prob_home, prob_away, n_books))
        self._odds_cache: dict[int, tuple[float, tuple[float, float, int]]] = {}

    # ── Public API ─────────────────────────────────────────────────────────

    def get_live_fixtures(self) -> list[SMFixture]:
        """Return all in-play fixtures with scores, xG, events. Cached 30s."""
        now = time.time()
        ts, cached = self._fixtures_cache
        if now - ts < CACHE_TTL and cached:
            return cached

        try:
            resp = httpx.get(
                f"{SPORTMONKS_BASE}/livescores/inplay",
                params={
                    "include": "scores;events;statistics;state;participants",
                    "api_token": self._api_key,
                },
                timeout=15,
            )
            resp.raise_for_status()
            raw = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("SportMonks livescores failed: %s", exc)
            return cached  # stale on error

        fixtures: list[SMFixture] = []
        for item in raw:
            f = self._parse_fixture(item)
            if f:
                fixtures.append(f)

        self._fixtures_cache = (now, fixtures)
        logger.debug("SportMonks: %d live fixtures", len(fixtures))
        return fixtures

    def get_live_odds(self, fixture_id: int) -> tuple[float, float, int]:
        """
        Return (prob_home, prob_away, n_bookmakers) — vig-normalized.
        Cached 30s. Returns (0.0, 0.0, 0) if unavailable.
        """
        now = time.time()
        if fixture_id in self._odds_cache:
            ts, result = self._odds_cache[fixture_id]
            if now - ts < CACHE_TTL:
                return result

        try:
            resp = httpx.get(
                f"{SPORTMONKS_BASE}/odds/inplay/fixtures/{fixture_id}",
                params={"api_token": self._api_key, "markets[]": "1"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as exc:
            logger.debug("SportMonks odds fixture %d: %s", fixture_id, exc)
            return (0.0, 0.0, 0)

        home_probs: list[float] = []
        away_probs: list[float] = []

        for bookmaker in data:
            for mkt in (bookmaker.get("markets") or []):
                for outcome in (mkt.get("outcomes") or []):
                    label = (
                        outcome.get("name") or outcome.get("label") or ""
                    ).strip().lower()
                    price = (
                        outcome.get("odds")
                        or outcome.get("price")
                        or outcome.get("value")
                        or 0
                    )
                    try:
                        price = float(price)
                        if price <= 1.0:
                            continue
                        prob = 1.0 / price
                    except (TypeError, ValueError, ZeroDivisionError):
                        continue

                    if label in ("home", "1", "home team", "1 (home)"):
                        home_probs.append(prob)
                    elif label in ("away", "2", "away team", "2 (away)"):
                        away_probs.append(prob)

        if not home_probs or not away_probs:
            result: tuple[float, float, int] = (0.0, 0.0, 0)
        else:
            avg_h = sum(home_probs) / len(home_probs)
            avg_a = sum(away_probs) / len(away_probs)
            total = avg_h + avg_a
            if total > 0:
                nh = round(avg_h / total, 4)
                na = round(avg_a / total, 4)
            else:
                nh = na = 0.0
            result = (nh, na, len(home_probs))

        self._odds_cache[fixture_id] = (now, result)
        return result

    # ── Parsing ────────────────────────────────────────────────────────────

    def _parse_fixture(self, f: dict) -> SMFixture | None:
        fid = f.get("id")
        if not fid:
            return None

        home_team = away_team = ""
        for p in (f.get("participants") or []):
            loc = (p.get("meta") or {}).get("location", "")
            name = p.get("name", "")
            if loc == "home":
                home_team = name
            elif loc == "away":
                away_team = name

        if not home_team or not away_team:
            return None

        home_score = away_score = 0
        for s in (f.get("scores") or []):
            if (s.get("description") or "").upper() != "CURRENT":
                continue
            sd = s.get("score") or {}
            goals = int(sd.get("goals") or 0)
            if sd.get("participant") == "home":
                home_score = goals
            elif sd.get("participant") == "away":
                away_score = goals

        state = f.get("state") or {}
        minute = int(state.get("minute") or 0)
        sname = (
            state.get("developer_name") or state.get("short_name") or ""
        ).upper()
        if "2ND" in sname or sname in ("2H", "SH"):
            period = "2H"
        elif "1ST" in sname or sname in ("1H", "FH"):
            period = "1H"
        elif "HT" in sname:
            period = "HT"
        elif "ET" in sname or "EXTRA" in sname:
            period = "ET"
        else:
            period = sname[:4] or "?"

        home_xg = away_xg = 0.0
        for stat in (f.get("statistics") or []):
            if stat.get("type_id") not in _XG_TYPE_IDS:
                continue
            loc = (stat.get("location") or "").lower()
            raw_val = (stat.get("data") or {}).get("value") or stat.get("value") or 0
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                continue
            if loc == "home":
                home_xg = val
            elif loc == "away":
                away_xg = val

        last_goal_minute = 0
        for ev in (f.get("events") or []):
            if ev.get("type_id") in _GOAL_TYPE_IDS:
                m = int(ev.get("minute") or 0)
                if m > last_goal_minute:
                    last_goal_minute = m

        return SMFixture(
            fixture_id=fid,
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            minute=minute,
            period=period,
            home_xg=home_xg,
            away_xg=away_xg,
            last_goal_minute=last_goal_minute,
        )


# Module-level singleton
sm_client = SportMonksClient()
