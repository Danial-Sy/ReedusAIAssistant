"""corpus domain taxonomy: which shard family a passage lands in and which a query probes."""

from __future__ import annotations

from typing import Dict, List

DOMAINS: Dict[str, Dict[str, List[str]]] = {
    "games": {
        "triggers": ["game", "games", "gaming", "playthrough", "boss", "speedrun", "dlc",
                     "roguelike", "fps", "rpg", "mmo", "console", "steam", "playstation",
                     "xbox", "nintendo", "switch", "pc gaming", "co-op", "multiplayer",
                     "patch", "meta", "loadout", "build", "grind", "raid", "combo"],
        "aliases": ["video game", "indie game", "fighting game", "shooter"],
    },
    "anime_manga": {
        "triggers": ["anime", "manga", "shonen", "seinen", "shojo", "arc", "filler",
                     "subbed", "dubbed", "studio ghibli", "light novel", "otaku",
                     "naruto", "one piece", "bleach", "jujutsu", "chainsaw man"],
        "aliases": ["japanese animation", "webtoon"],
    },
    "film_tv": {
        "triggers": ["movie", "movies", "film", "films", "show", "series", "season",
                     "episode", "director", "cast", "actor", "actress", "netflix", "hbo",
                     "streaming", "cinema", "sequel", "trilogy", "documentary", "sitcom"],
        "aliases": ["tv show", "box office", "screenplay"],
    },
    "music": {
        "triggers": ["music", "album", "song", "track", "band", "artist", "gig", "concert",
                     "tour", "guitar", "bass", "drums", "synth", "vinyl", "playlist",
                     "producer", "remix", "genre", "riff", "setlist"],
        "aliases": ["record", "ep", "single", "discography"],
    },
    "sport": {
        "triggers": ["football", "soccer", "basketball", "nba", "nfl", "mlb", "f1",
                     "formula 1", "tennis", "cricket", "rugby", "boxing", "ufc", "mma",
                     "league", "cup", "fixture", "squad", "transfer", "manager", "coach"],
        "aliases": ["premier league", "world cup", "playoffs", "grand prix"],
    },
    "tech": {
        "triggers": ["gpu", "cpu", "laptop", "phone", "hardware", "software", "framework",
                     "library", "language", "compiler", "database", "server", "linux",
                     "python", "rust", "javascript", "docker", "kubernetes", "api",
                     "keyboard", "monitor", "build a pc"],
        "aliases": ["programming", "developer", "open source"],
    },
    "food_drink": {
        "triggers": ["recipe", "cook", "cooking", "bake", "baking", "restaurant", "dish",
                     "cuisine", "coffee", "beer", "wine", "whisky", "cocktail", "sourdough",
                     "steak", "pizza", "ramen", "sauce", "ingredient"],
        "aliases": ["chef", "menu", "brewing"],
    },
    "hobbies": {
        "triggers": ["climbing", "bouldering", "running", "cycling", "gym", "lifting",
                     "knitting", "painting", "drawing", "photography", "camera", "lens",
                     "warhammer", "miniatures", "board game", "chess", "fishing", "hiking",
                     "woodworking", "gardening", "plants"],
        "aliases": ["craft", "workout", "training"],
    },
    "general": {
        "triggers": [],
        "aliases": [],
    },
}

FALLBACK = "general"
MAX_DOMAINS_PER_QUERY = 2          # mirrors vision's max_groups_per_request


def names() -> List[str]:
    return list(DOMAINS)


def triggers(domain: str) -> List[str]:
    d = DOMAINS[domain]
    return d["triggers"] + d["aliases"]


def validate() -> None:
    """Every domain distinct, no trigger claimed by two domains (ambiguity is a routing bug)."""
    seen: Dict[str, str] = {}
    for name, d in DOMAINS.items():
        for t in d["triggers"]:
            if t in seen:
                raise ValueError(f"trigger {t!r} claimed by both {seen[t]!r} and {name!r}")
            seen[t] = name
