"""
Live Score Guard — checks Odds API /scores before entering soccer markets.

Logic:
  - Game not started       → safe to enter (pre-match odds valid)
  - Game live, score 0-0   → safe to enter (odds still close to pre-match)
  - Game live, has a goal  → SKIP (market already reflects live info)
  - Game completed         → SKIP
"""

import logging
import os
import time

import httpx

logger = logging.getLogger("polybot.scores")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
CACHE_TTL = 60  # seconds between API calls per sport

# Polymarket category → Odds API sport key
SOCCER_SPORT_MAP = {
    "ucl":    "soccer_uefa_champs_league",
    "soccer": "soccer_epl",
    "epl":    "soccer_epl",
    "laliga": "soccer_spain_la_liga",
    "bundesliga": "soccer_germany_bundesliga",
    "seriea": "soccer_italy_serie_a",
    "ligue1": "soccer_france_ligue_one",
}

# Categories treated as soccer (require live score check)
SOCCER_CATEGORIES = set(SOCCER_SPORT_MAP.keys())

# Fallback: check all major leagues if category not mapped
FALLBACK_SPORTS = [
    "soccer_uefa_champs_league",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
]


class LiveScoreCache:
    """Caches live scores from Odds API with TTL to avoid excessive API calls."""

    def __init__(self):
        self._cache: dict[str, tuple[float, list]] = {}  # sport -> (ts, games)
        self._api_key = os.getenv("ODDS_API_KEY", "")

    def _fetch(self, sport: str) -> list:
        """Fetch scores for a sport, using cache if fresh."""
        now = time.time()
        cached = self._cache.get(sport)
        if cached and (now - cached[0]) < CACHE_TTL:
            return cached[1]

        try:
            resp = httpx.get(
                f"{ODDS_API_BASE}/sports/{sport}/scores",
                params={"apiKey": self._api_key, "daysFrom": 1},
                timeout=8,
            )
            if resp.status_code == 200:
                games = resp.json()
                self._cache[sport] = (now, games)
                logger.debug("Scores fetched: %s → %d games", sport, len(games))
                return games
            else:
                logger.warning("Scores API %s returned %d", sport, resp.status_code)
        except Exception as exc:
            logger.warning("Scores fetch failed %s: %s", sport, exc)

        # Return stale data if available
        return self._cache.get(sport, (0, []))[1]

    def _team_in_question(self, team: str, question: str) -> bool:
        """Fuzzy match: check if significant parts of team name appear in question."""
        q = question.lower()
        team_lower = team.lower()

        # Direct match
        if team_lower in q:
            return True

        # Match meaningful words (>3 chars) from team name
        words = [w for w in team_lower.split() if len(w) > 3
                 and w not in ("city", "club", "team", "sport", "united", "sporting")]
        return any(w in q for w in words)

    def is_live_with_goal(self, question: str, category: str) -> bool:
        """
        Returns True if a soccer match is live AND has a non-zero score.
        In that case, pre-match odds are stale and the bot should skip entry.
        """
        # Determine which sport(s) to check
        sport = SOCCER_SPORT_MAP.get(category)
        sports = [sport] if sport else FALLBACK_SPORTS

        for sport_key in sports:
            games = self._fetch(sport_key)
            for game in games:
                if game.get("completed"):
                    continue  # already finished, Polymarket will resolve soon

                scores = game.get("scores") or []
                if not scores:
                    continue  # not started yet — safe

                home = game.get("home_team", "")
                away = game.get("away_team", "")

                # Check if this game matches the Polymarket question
                if not (self._team_in_question(home, question) or
                        self._team_in_question(away, question)):
                    continue

                # Game found — check score
                try:
                    score_map = {s["name"]: int(s["score"]) for s in scores}
                    home_score = score_map.get(home, 0)
                    away_score = score_map.get(away, 0)

                    if home_score > 0 or away_score > 0:
                        logger.info(
                            "SCORE GUARD: '%s' live %d-%d → skip",
                            question[:50], home_score, away_score,
                        )
                        return True  # live + goal → skip
                    else:
                        logger.debug(
                            "SCORE GUARD: '%s' live 0-0 → ok to enter",
                            question[:50],
                        )
                        return False  # live but 0-0 → ok

                except (KeyError, ValueError):
                    continue

        return False  # no match found → assume safe (pre-match)


# Module-level singleton — shared across all paper trader cycles
score_cache = LiveScoreCache()
