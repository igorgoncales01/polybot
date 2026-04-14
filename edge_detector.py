"""
Edge Detector — compares Polymarket prices with real odds from:
  1. The Odds API (traditional sports: NHL, NBA, Soccer, Tennis, MMA)
  2. PandaScore (esports: LoL, CS2, Valorant, Dota 2)

Only recommends entry when Polymarket price is significantly below
the implied fair value from external sources.
"""

import os
import time
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("polybot.edge")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
PANDASCORE_KEY = os.getenv("PANDASCORE_KEY", "")

# Minimum edge (in cents) to justify entry
MIN_EDGE_CENTS = float(os.getenv("MIN_EDGE_CENTS", "5.0"))

# Cache TTL in seconds (avoid burning API credits)
ODDS_CACHE_TTL = 60      # The Odds API: refresh every 60s
PANDA_CACHE_TTL = 15      # PandaScore: refresh every 15s (free-ish)


@dataclass
class EdgeSignal:
    """Result of edge analysis for a candidate."""
    has_edge: bool
    edge_cents: float          # positive = undervalued on Polymarket
    fair_price: float          # implied probability from external source
    poly_price: float          # current Polymarket price
    source: str                # "odds_api" or "pandascore"
    details: str               # human-readable explanation
    confidence: str            # "high", "medium", "low"


# ── Caches ────────────────────────────────────────────────────────

_odds_cache: dict = {}         # sport_key -> {data, timestamp}
_panda_live_cache: dict = {}   # {data, timestamp}
_panda_upcoming_cache: dict = {}


def _cached_odds(sport_key: str) -> list[dict]:
    """Fetch odds from The Odds API with caching."""
    if not ODDS_API_KEY:
        return []
    now = time.time()
    cached = _odds_cache.get(sport_key)
    if cached and now - cached["timestamp"] < ODDS_CACHE_TTL:
        return cached["data"]

    try:
        resp = httpx.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us,eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _odds_cache[sport_key] = {"data": data, "timestamp": now}
        remaining = resp.headers.get("x-requests-remaining", "?")
        logger.info("Odds API %s: %d games, credits remaining: %s", sport_key, len(data), remaining)
        return data
    except Exception as e:
        logger.error("Odds API error for %s: %s", sport_key, e)
        return cached["data"] if cached else []


def _cached_pandascore_live() -> list[dict]:
    """Fetch live esports matches from PandaScore with caching."""
    if not PANDASCORE_KEY:
        return []
    now = time.time()
    if _panda_live_cache and now - _panda_live_cache.get("timestamp", 0) < PANDA_CACHE_TTL:
        return _panda_live_cache.get("data", [])

    try:
        resp = httpx.get(
            "https://api.pandascore.co/matches/running",
            params={"token": PANDASCORE_KEY, "per_page": 50},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _panda_live_cache["data"] = data
        _panda_live_cache["timestamp"] = now
        logger.info("PandaScore live: %d matches", len(data))
        return data
    except Exception as e:
        logger.error("PandaScore error: %s", e)
        return _panda_live_cache.get("data", [])


def _cached_pandascore_upcoming() -> list[dict]:
    """Fetch upcoming esports matches from PandaScore."""
    if not PANDASCORE_KEY:
        return []
    now = time.time()
    if _panda_upcoming_cache and now - _panda_upcoming_cache.get("timestamp", 0) < PANDA_CACHE_TTL:
        return _panda_upcoming_cache.get("data", [])

    try:
        resp = httpx.get(
            "https://api.pandascore.co/matches/upcoming",
            params={"token": PANDASCORE_KEY, "per_page": 50},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _panda_upcoming_cache["data"] = data
        _panda_upcoming_cache["timestamp"] = now
        return data
    except Exception as e:
        logger.error("PandaScore upcoming error: %s", e)
        return _panda_upcoming_cache.get("data", [])


# ── Team name matching ────────────────────────────────────────────

def _normalize(name: str) -> str:
    """Normalize team name for fuzzy matching."""
    name = name.lower().strip()
    # Replace hyphens/dashes with spaces BEFORE removing suffixes
    name = name.replace("-", " ").replace("–", " ").replace("—", " ")
    # Remove common suffixes
    for suffix in [" fc", " esports", " gaming", " team", " esport"]:
        name = name.replace(suffix, "")
    # Remove special chars (keep spaces)
    name = re.sub(r'[^a-z0-9 ]', '', name)
    # Collapse multiple spaces
    name = re.sub(r' +', ' ', name)
    return name.strip()


def _names_match(name1: str, name2: str) -> bool:
    """Check if two team names refer to the same team."""
    n1 = _normalize(name1)
    n2 = _normalize(name2)
    if n1 == n2:
        return True
    # Partial match (one contains the other)
    if len(n1) > 2 and len(n2) > 2:
        if n1 in n2 or n2 in n1:
            return True
    # Check key words overlap
    words1 = set(n1.split())
    words2 = set(n2.split())
    if words1 and words2 and words1 & words2:
        return True
    return False


# ── Polymarket category → Odds API sport key mapping ──────────────

POLY_TO_ODDS_SPORT = {
    # Ice Hockey
    "nhl": "icehockey_nhl",
    "stanley cup": "icehockey_nhl_championship_winner",
    # Basketball
    "nba": "basketball_nba",
    "nba finals": "basketball_nba_championship_winner",
    "euroleague": "basketball_euroleague",
    # Soccer — Major Leagues
    "epl": "soccer_epl",
    "premier league": "soccer_epl",
    "la liga": "soccer_spain_la_liga",
    "serie a": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue 1": "soccer_france_ligue_one",
    "ligue one": "soccer_france_ligue_one",
    "eredivisie": "soccer_netherlands_eredivisie",
    "primeira liga": "soccer_portugal_primeira_liga",
    "super league": "soccer_turkey_super_league",
    "mls": "soccer_usa_mls",
    # Soccer — Cups & International
    "ucl": "soccer_uefa_champs_league",
    "champions league": "soccer_uefa_champs_league",
    "europa league": "soccer_uefa_europa_league",
    "conference league": "soccer_uefa_europa_conference_league",
    "fa cup": "soccer_fa_cup",
    "copa del rey": "soccer_spain_copa_del_rey",
    "coppa italia": "soccer_italy_coppa_italia",
    "dfb pokal": "soccer_germany_dfb_pokal",
    "coupe de france": "soccer_france_coupe_de_france",
    "fifa world cup": "soccer_fifa_world_cup",
    "copa libertadores": "soccer_conmebol_copa_libertadores",
    "copa sudamericana": "soccer_conmebol_copa_sudamericana",
    "liga mx": "soccer_mexico_ligamx",
    # Soccer — Secondary Leagues
    "championship": "soccer_efl_champ",
    "saudi pro": "soccer_saudi_arabia_pro_league",
    "j league": "soccer_japan_j_league",
    "k league": "soccer_korea_kleague1",
    "a-league": "soccer_australia_aleague",
    "superliga": "soccer_denmark_superliga",
    "allsvenskan": "soccer_sweden_allsvenskan",
    "eliteserien": "soccer_norway_eliteserien",
    "serie b": "soccer_brazil_serie_b",
    # Combat Sports
    "mma": "mma_mixed_martial_arts",
    "ufc": "mma_mixed_martial_arts",
    "boxing": "boxing_boxing",
    # Tennis
    "tennis": "tennis_atp_barcelona_open",
    "atp": "tennis_atp_barcelona_open",
    "wta": "tennis_wta_stuttgart_open",
    # Cricket
    "cricket": "cricket_ipl",
    "ipl": "cricket_ipl",
    # Baseball
    "mlb": "baseball_mlb",
    # Other
    "nfl": "americanfootball_nfl",
    "golf": "golf_pga_championship_winner",
    "rugby": "rugbyleague_nrl",
    # Team-name triggers (fallback)
    "paris saint": "soccer_uefa_champs_league",
    "barcelona": "soccer_uefa_champs_league",
    "liverpool": "soccer_uefa_champs_league",
    "arsenal": "soccer_uefa_champs_league",
    "manchester united": "soccer_epl",
    "manchester city": "soccer_epl",
    "real madrid": "soccer_spain_la_liga",
    "juventus": "soccer_italy_serie_a",
    "bayern munich": "soccer_germany_bundesliga",
    "inter milan": "soccer_italy_serie_a",
}

# Esports identifiers in Polymarket questions
ESPORTS_KEYWORDS = [
    # Games
    "lol", "cs2", "csgo", "counter-strike", "valorant", "dota", "league of legends",
    "rainbow 6", "r6", "rocket league", "call of duty", "cod", "overwatch", "pubg",
    "starcraft", "mobile legends", "king of glory", "wild rift", "ea fc",
    # Leagues
    "lec", "lck", "lpl", "lcs", "cblol", "vct", "iem", "esl", "blast",
    "dreamleague", "the international", "worlds", "msi",
    # Teams (common in Polymarket)
    "fnatic", "g2", "navi", "natus vincere", "vitality", "giantx", "sk gaming",
    "t1", "gen.g", "cloud9", "team liquid", "sentinels", "loud", "drx",
    "heroic", "faze", "mouz", "astralis", "big", "falcons",
    "b8", "3dmax", "virtus.pro",
]


def _is_esports(question: str, category: str) -> bool:
    """Check if a market is esports."""
    text = f"{question} {category}".lower()
    return any(kw in text for kw in ESPORTS_KEYWORDS)


# ── Edge detection: Traditional Sports ────────────────────────────

def _find_odds_edge(outcome: str, question: str, category: str, poly_price: float) -> Optional[EdgeSignal]:
    """Compare Polymarket price with bookmaker odds."""
    # Map category to odds API sport — search in category + question + outcome
    sport_key = None
    searchable = f"{category} {question} {outcome}".lower()
    for poly_cat, odds_sport in POLY_TO_ODDS_SPORT.items():
        if poly_cat in searchable:
            sport_key = odds_sport
            break

    if not sport_key:
        return None

    games = _cached_odds(sport_key)
    if not games:
        return None

    best_match = None
    best_fair_price = 0

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Check if the outcome matches a team in this game
        matched_team = None
        if _names_match(outcome, home):
            matched_team = home
        elif _names_match(outcome, away):
            matched_team = away
        elif outcome.lower() == "yes":
            # "Will X win?" markets — check question for team name
            for team in [home, away]:
                if _normalize(team) in _normalize(question):
                    # Get this team's win probability directly
                    probs = []
                    for bk in game.get("bookmakers", []):
                        for mkt in bk.get("markets", []):
                            for o in mkt.get("outcomes", []):
                                if _names_match(o["name"], team):
                                    probs.append(1 / o["price"])
                    if probs:
                        avg_fair = sum(probs) / len(probs)
                        if avg_fair > best_fair_price:
                            best_fair_price = avg_fair
                            best_match = f"Yes {team} (avg {len(probs)} bookmakers)"
                    break
        elif outcome.lower() == "no":
            # "Will X win?" — No = the other team or draw
            for team in [home, away]:
                if _normalize(team) in _normalize(question):
                    # "No" on "Will Man United win?" = implied prob of NOT winning
                    # Get the team's win prob, then fair = 1 - win_prob
                    for bk in game.get("bookmakers", []):
                        for mkt in bk.get("markets", []):
                            for o in mkt.get("outcomes", []):
                                if _names_match(o["name"], team):
                                    win_prob = 1 / o["price"]
                                    no_fair = 1 - win_prob
                                    if no_fair > best_fair_price:
                                        best_fair_price = no_fair
                                        best_match = f"No {team} (via {bk['key']})"
                    break

        if matched_team and outcome.lower() not in ["yes", "no"]:
            # Direct team match — get average implied probability across bookmakers
            probs = []
            for bk in game.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    for o in mkt.get("outcomes", []):
                        if _names_match(o["name"], matched_team):
                            implied = 1 / o["price"]
                            probs.append(implied)

            if probs:
                avg_fair = sum(probs) / len(probs)
                if avg_fair > best_fair_price:
                    best_fair_price = avg_fair
                    best_match = f"{matched_team} (avg {len(probs)} bookmakers)"

    if best_fair_price > 0:
        edge_cents = (best_fair_price - poly_price) * 100
        return EdgeSignal(
            has_edge=edge_cents >= MIN_EDGE_CENTS,
            edge_cents=round(edge_cents, 1),
            fair_price=round(best_fair_price, 4),
            poly_price=poly_price,
            source="odds_api",
            details=best_match or "bookmaker consensus",
            confidence="high" if edge_cents >= 10 else "medium" if edge_cents >= 5 else "low",
        )

    return None


# ── Edge detection: Esports (PandaScore) ──────────────────────────

def _estimate_bo3_prob(score_a: int, score_b: int, base_prob: float = 0.5) -> float:
    """
    Estimate win probability in a BO3 given current score.
    Uses simple model: if leading 1-0, chance is significantly higher.
    """
    if score_a >= 2:
        return 0.95  # Already won
    if score_b >= 2:
        return 0.05  # Already lost
    if score_a == 1 and score_b == 0:
        return 0.70  # Leading 1-0 in BO3
    if score_a == 0 and score_b == 1:
        return 0.30  # Trailing 0-1 in BO3
    if score_a == 1 and score_b == 1:
        return 0.50  # Tied 1-1, coin flip
    return base_prob  # 0-0


def _estimate_bo5_prob(score_a: int, score_b: int) -> float:
    """Estimate win probability in a BO5."""
    if score_a >= 3:
        return 0.95
    if score_b >= 3:
        return 0.05
    diff = score_a - score_b
    # Simple lookup
    prob_map = {
        (2, 0): 0.85, (0, 2): 0.15,
        (2, 1): 0.70, (1, 2): 0.30,
        (1, 0): 0.65, (0, 1): 0.35,
        (1, 1): 0.50,
    }
    return prob_map.get((score_a, score_b), 0.50)


def _find_pandascore_edge(outcome: str, question: str, category: str, poly_price: float) -> Optional[EdgeSignal]:
    """Compare Polymarket price with PandaScore live scores for esports."""
    if not _is_esports(question, category):
        return None

    live_matches = _cached_pandascore_live()
    if not live_matches:
        return None

    for match in live_matches:
        opponents = match.get("opponents", [])
        if len(opponents) < 2:
            continue

        team_a = opponents[0].get("opponent", {}).get("name", "")
        team_b = opponents[1].get("opponent", {}).get("name", "")

        # Check if our outcome matches either team
        our_team_idx = None
        if _names_match(outcome, team_a):
            our_team_idx = 0
        elif _names_match(outcome, team_b):
            our_team_idx = 1
        else:
            continue

        # Get scores
        results = match.get("results", [])
        if len(results) < 2:
            continue

        score_a = results[0].get("score", 0)
        score_b = results[1].get("score", 0)

        # Determine BO format
        num_games = match.get("number_of_games", 3)
        if num_games >= 5:
            our_score = score_a if our_team_idx == 0 else score_b
            opp_score = score_b if our_team_idx == 0 else score_a
            fair_prob = _estimate_bo5_prob(our_score, opp_score)
        else:
            our_score = score_a if our_team_idx == 0 else score_b
            opp_score = score_b if our_team_idx == 0 else score_a
            fair_prob = _estimate_bo3_prob(our_score, opp_score)

        edge_cents = (fair_prob - poly_price) * 100
        game_status = f"Score {our_score}-{opp_score} in BO{num_games}"

        # Check which game is running
        games = match.get("games", [])
        current_game = sum(1 for g in games if g.get("status") in ("finished", "running"))
        game_status += f" (Game {current_game}/{num_games})"

        league = match.get("league", {}).get("name", "")
        videogame = match.get("videogame", {}).get("name", "")

        return EdgeSignal(
            has_edge=edge_cents >= MIN_EDGE_CENTS,
            edge_cents=round(edge_cents, 1),
            fair_price=round(fair_prob, 4),
            poly_price=poly_price,
            source="pandascore",
            details=f"{videogame} {league}: {team_a} vs {team_b} — {game_status}",
            confidence="high" if abs(edge_cents) >= 15 else "medium" if abs(edge_cents) >= 5 else "low",
        )

    return None


# ── Public API ────────────────────────────────────────────────────

def check_edge(outcome: str, question: str, category: str, poly_price: float) -> Optional[EdgeSignal]:
    """
    Check if a Polymarket candidate has edge vs external sources.
    Returns EdgeSignal if analysis found, None if no data available.
    """
    # Try esports first (PandaScore — free, fast)
    if _is_esports(question, category):
        signal = _find_pandascore_edge(outcome, question, category, poly_price)
        if signal:
            return signal

    # Try traditional sports (Odds API — uses credits)
    signal = _find_odds_edge(outcome, question, category, poly_price)
    if signal:
        return signal

    # No external data found
    return None


def get_all_live_edges(candidates: list) -> list[dict]:
    """
    Batch check all candidates for edge. Returns list of candidates with edge info.
    Used by the dashboard for display.
    """
    results = []
    for c in candidates:
        signal = check_edge(
            outcome=c.get("outcome", ""),
            question=c.get("question", ""),
            category=c.get("category", ""),
            poly_price=c.get("price", 0),
        )
        result = {**c}
        if signal:
            result["edge"] = {
                "has_edge": signal.has_edge,
                "edge_cents": signal.edge_cents,
                "fair_price": signal.fair_price,
                "source": signal.source,
                "details": signal.details,
                "confidence": signal.confidence,
            }
        results.append(result)
    return results
