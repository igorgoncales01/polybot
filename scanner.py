"""
Market scanner for the Underdog Bounce strategy.
Uses the Gamma API (gamma-api.polymarket.com) which returns live active markets,
then enriches with CLOB token IDs from the CLOB API.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from config import (
    CLOB_API_URL,
    MIN_PRICE,
    MAX_PRICE,
    MIN_LIQUIDITY,
    FILTER_CATEGORIES,
    MAX_HOURS_TO_END,
)

logger = logging.getLogger("polybot.scanner")

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Extended keywords to match sport slugs/questions
SPORT_KEYWORDS = [
    # Soccer
    "soccer", "epl", "premier-league", "la-liga", "serie-a", "bundesliga",
    "champions-league", "ucl", "europa-league", "mls", "copa", "fifa",
    "fa-cup", "liga-mx", "eredivisie", "primeira-liga", "ligue-1",
    "saudi", "j-league", "k-league", "a-league", "superliga",
    # Basketball
    "nba", "basketball", "ncaab", "euroleague",
    # Hockey
    "nhl", "hockey", "ice-hockey", "stanley-cup",
    # Tennis
    "tennis", "atp", "wta", "grand-slam", "wimbledon", "roland-garros",
    # Combat
    "mma", "ufc", "boxing",
    # Baseball
    "mlb", "baseball",
    # Cricket
    "cricket", "ipl",
    # Golf / F1 / Rugby
    "golf", "pga", "f1", "formula", "rugby", "nfl",
    # Esports
    "esports", "cs2", "csgo", "counter-strike", "dota", "league-of-legends",
    "lol", "valorant", "lec", "lck", "lpl", "vct", "iem", "blast",
    "dreamleague", "rainbow-6", "rocket-league", "overwatch", "pubg",
    # Teams (catch esports by team name)
    "fnatic", "g2", "navi", "natus-vincere", "vitality", "giantx",
    "t1", "gen.g", "cloud9", "team-liquid", "sentinels", "loud",
    "faze", "mouz", "astralis", "heroic", "falcons", "b8", "3dmax",
]


@dataclass
class Candidate:
    """An underdog outcome that passed all filters."""

    condition_id: str
    token_id: str
    question: str
    outcome: str
    price: float
    liquidity: float
    category: str
    volume_24h: float = 0
    end_date: str = ""


def _fetch_gamma_page(offset: int = 0, limit: int = 100) -> list[dict]:
    """Fetch a page of active markets from the Gamma API."""
    resp = httpx.get(
        f"{GAMMA_API_URL}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("data", [])


def _matches_sport(market: dict) -> bool:
    """Check if the market matches target sport categories."""
    slug = (market.get("slug") or market.get("market_slug") or "").lower()
    question = (market.get("question") or "").lower()
    description = (market.get("description") or "").lower()
    searchable = f"{slug} {question} {description}"
    return any(kw in searchable for kw in SPORT_KEYWORDS)


def _is_live_market(market: dict) -> bool:
    """Check if market ends within MAX_HOURS_TO_END hours."""
    if MAX_HOURS_TO_END <= 0:
        return True  # filter disabled
    end_raw = market.get("endDateIso") or market.get("endDate") or ""
    if not end_raw:
        return False
    try:
        # Handle multiple date formats: "2026-04-13", "2026-04-13T...", ISO with tz
        end_str = end_raw.strip()
        if "T" in end_str:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            # Plain date like "2026-04-13" — treat as end of day UTC
            end_dt = datetime.strptime(end_str[:10], "%Y-%m-%d").replace(
                hour=23, minute=59, tzinfo=timezone.utc
            )
        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return 0 < hours_left <= MAX_HOURS_TO_END
    except (ValueError, TypeError):
        return False


def _extract_candidates(market: dict, min_liq: float = 0) -> list[Candidate]:
    """Return qualifying underdog outcomes from a Gamma market."""
    candidates: list[Candidate] = []
    condition_id = market.get("conditionId") or market.get("condition_id", "")
    question = market.get("question", "unknown")
    slug = market.get("slug") or market.get("market_slug") or ""

    # Live market filter
    if not _is_live_market(market):
        return []

    liquidity = float(market.get("liquidity", 0) or 0)
    if liquidity < min_liq:
        return []

    volume_24h = float(market.get("volume24hr") or market.get("volume24hrClob") or 0)
    end_date = (market.get("endDateIso") or market.get("endDate") or "")[:10]

    try:
        outcome_prices = json.loads(market.get("outcomePrices") or "[]")
    except (json.JSONDecodeError, TypeError):
        outcome_prices = []

    try:
        token_ids = json.loads(market.get("clobTokenIds") or "[]")
    except (json.JSONDecodeError, TypeError):
        token_ids = []

    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, TypeError):
            outcomes = []

    for i, price_str in enumerate(outcome_prices):
        price = float(price_str)
        if MIN_PRICE <= price <= MAX_PRICE:
            token_id = token_ids[i] if i < len(token_ids) else ""
            outcome = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
            if token_id:
                candidates.append(
                    Candidate(
                        condition_id=condition_id,
                        token_id=str(token_id),
                        question=question,
                        outcome=str(outcome),
                        price=price,
                        liquidity=liquidity,
                        category=slug.split("-")[0] if slug else "",
                        volume_24h=volume_24h,
                        end_date=end_date,
                    )
                )
    return candidates


def _fetch_hot_markets() -> list[dict]:
    """Fetch markets sorted by 24h volume (most active first)."""
    try:
        resp = httpx.get(
            f"{GAMMA_API_URL}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except httpx.HTTPError as exc:
        logger.error("Hot markets fetch failed: %s", exc)
        return []


def scan_markets(max_pages: int = 10) -> list[Candidate]:
    """
    Two-pass scan:
    1. HOT markets — sorted by 24h volume (live action, fast-moving)
    2. Regular pagination — fills remaining slots
    Candidates are deduplicated and sorted by volume (hottest first).
    """
    seen_tokens = set()
    all_candidates: list[Candidate] = []

    # Pass 1: Hot markets (highest 24h volume, lower liquidity threshold)
    hot_markets = _fetch_hot_markets()
    hot_count = 0
    for market in hot_markets:
        vol24 = float(market.get("volume24hr") or 0)
        if vol24 < 500:
            continue
        if not _is_live_market(market):
            continue
        if FILTER_CATEGORIES and not _matches_sport(market):
            continue
        for c in _extract_candidates(market, min_liq=1000):
            if c.token_id not in seen_tokens:
                seen_tokens.add(c.token_id)
                all_candidates.append(c)
                hot_count += 1

    # Pass 2: Regular scan for breadth
    page_size = 100
    offset = 0
    for page in range(max_pages):
        try:
            markets = _fetch_gamma_page(offset=offset, limit=page_size)
        except httpx.HTTPError as exc:
            logger.error("Gamma API page %d failed: %s", page, exc)
            break

        if not markets:
            break

        for market in markets:
            if FILTER_CATEGORIES and not _matches_sport(market):
                continue
            for c in _extract_candidates(market, min_liq=MIN_LIQUIDITY):
                if c.token_id not in seen_tokens:
                    seen_tokens.add(c.token_id)
                    all_candidates.append(c)

        offset += page_size
        if len(markets) < page_size:
            break

    # Sort: highest volume first (live/hot markets get priority)
    all_candidates.sort(key=lambda c: -c.volume_24h)

    logger.info(
        "Scan: %d hot + %d total candidates (deduped)",
        hot_count, len(all_candidates),
    )
    return all_candidates
