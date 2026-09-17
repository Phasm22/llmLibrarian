"""Reverse geocoding for photo GPS coordinates.

EXIF gives us a latitude/longitude pair, which no language model can turn into a
venue on its own. Asking a keyword web search engine to do it does not work
either -- raw coordinates are the worst possible query for a term-matching index
(a real "Korean bento near 38.9544, -104.7284" search returned restaurants in
Toronto, Baku and Positano). Resolving the coordinate once, at ingest, against an
actual geocoder puts a real place name in the chunk text where retrieval can find
it.

This is OFF by default and gated behind LLMLIBRARIAN_REVERSE_GEOCODE=1, because
it is the only part of ingestion that sends anything off-box. The coordinates of
a personal photo are sensitive -- they are usually someone's home, workplace, or
their child's school -- so turning this on is the user's call, not ours.

Results are cached on disk forever: a coordinate's address does not change, and
the cache is what keeps Nominatim's 1 req/sec policy cheap to honor across
re-ingests.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Phone GPS indoors is routinely 10-30 m off, so the containing feature at the
# exact coordinate is often a parking lot or the retail park rather than the
# venue. (The photo that motivated this resolved to "Powers Center Point" -- a
# parking polygon -- while the restaurant sat 14 m away.) Searching a small box
# around the point and sorting by distance recovers the actual venue.
_NEARBY_RADIUS_M = 150.0
_DEFAULT_NEARBY_CATEGORIES = ("restaurant", "cafe", "bar", "hotel")
_MAX_NEARBY = 4

# OSM's catch-all tag values, which carry no information as a category.
_UNINFORMATIVE_CATEGORIES = {"yes", "no", "unclassified"}

# Nominatim's usage policy requires an identifying User-Agent and at most one
# request per second. Both are conditions of the free service, not suggestions.
_USER_AGENT = "llmLibrarian/1.0 (personal knowledge indexer)"
_MIN_INTERVAL_S = 1.1

# Coordinates are cached at ~11 m precision (5 decimal places). Finer than that
# is GPS noise, and rounding lets two photos of the same table share one lookup.
_CACHE_PRECISION = 5

_rate_lock = threading.Lock()
_last_request_at = 0.0


def reverse_geocode_enabled() -> bool:
    """True when the user has opted in to off-box coordinate lookups."""
    raw = (os.environ.get("LLMLIBRARIAN_REVERSE_GEOCODE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _cache_path() -> Path:
    override = os.environ.get("LLMLIBRARIAN_GEOCODE_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pal" / "geocode-cache.json"


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=0, sort_keys=True)
        tmp.replace(path)
    except OSError as exc:
        logger.debug("geocode cache write failed: %s", exc)


def _cache_key(latitude: float, longitude: float) -> str:
    return f"{round(latitude, _CACHE_PRECISION)},{round(longitude, _CACHE_PRECISION)}"


def _throttle() -> None:
    global _last_request_at
    with _rate_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _MIN_INTERVAL_S:
            time.sleep(_MIN_INTERVAL_S - elapsed)
        _last_request_at = time.monotonic()


def _summarize(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Nominatim response to the few fields worth indexing.

    `name` is only set when the coordinate lands on a named feature (a business,
    a park). A coordinate in a parking lot resolves to the surrounding retail
    area instead, which is still a useful anchor even though it is not a venue.
    """
    address = payload.get("address") or {}
    name = (payload.get("name") or "").strip()
    display = (payload.get("display_name") or "").strip()

    parts = [
        address.get("road") or address.get("retail") or address.get("neighbourhood"),
        address.get("city") or address.get("town") or address.get("village"),
        address.get("state"),
        address.get("postcode"),
    ]
    short = ", ".join(str(p) for p in parts if p)

    result: dict[str, Any] = {}
    if name:
        result["place_name"] = name
    if short:
        result["place_address"] = short
    elif display:
        result["place_address"] = display
    category = payload.get("type") or payload.get("category")
    # OSM tags a plain building as type="yes"; recording that as a category adds
    # a field that says nothing.
    if category and str(category) not in _UNINFORMATIVE_CATEGORIES:
        result["place_category"] = str(category)
    return result


def _nearby_categories() -> tuple[str, ...]:
    raw = os.environ.get("LLMLIBRARIAN_NEARBY_CATEGORIES")
    if raw is None:
        return _DEFAULT_NEARBY_CATEGORIES
    # An explicit empty value disables the nearby pass while leaving the plain
    # reverse lookup on -- one request per photo instead of five.
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation. Exact enough over a 150 m box."""
    import math

    dy = (lat2 - lat1) * 111320.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def _fetch_json(url: str, params: dict[str, str]) -> Any:
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        _throttle()
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("nominatim request failed (%s): %s", url, exc)
        return None


def _nearby_places(latitude: float, longitude: float) -> list[dict[str, Any]]:
    """Named venues within the search box, nearest first.

    Nominatim has no "everything near here" endpoint -- a bounded search needs a
    term, and only its special phrases (``restaurant``, ``cafe``, ...) resolve to
    a category. So we ask once per configured category and merge the answers.
    """
    categories = _nearby_categories()
    if not categories:
        return []

    import math

    dlat = _NEARBY_RADIUS_M / 111320.0
    dlon = _NEARBY_RADIUS_M / (111320.0 * max(math.cos(math.radians(latitude)), 1e-6))
    viewbox = f"{longitude - dlon:.6f},{latitude + dlat:.6f},{longitude + dlon:.6f},{latitude - dlat:.6f}"

    found: dict[str, dict[str, Any]] = {}
    for category in categories:
        payload = _fetch_json(
            NOMINATIM_SEARCH_URL,
            {
                "q": category,
                "format": "jsonv2",
                "limit": "10",
                "bounded": "1",
                "viewbox": viewbox,
            },
        )
        if not isinstance(payload, list):
            continue
        for entry in payload:
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            try:
                entry_lat = float(entry["lat"])
                entry_lon = float(entry["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            distance = _distance_m(latitude, longitude, entry_lat, entry_lon)
            if distance > _NEARBY_RADIUS_M:
                continue
            existing = found.get(name)
            if existing is None or distance < existing["distance_m"]:
                found[name] = {
                    "name": name,
                    "category": entry.get("type") or category,
                    "distance_m": round(distance),
                }

    ranked = sorted(found.values(), key=lambda item: item["distance_m"])
    return ranked[:_MAX_NEARBY]


def reverse_geocode(latitude: float, longitude: float) -> dict[str, Any]:
    """Resolve a coordinate to a place. Returns {} when disabled or unavailable.

    Never raises: a geocoder that is off, offline, rate-limited or slow must not
    be able to fail an ingest run. The photo still indexes with raw coordinates.
    """
    if not reverse_geocode_enabled():
        return {}
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return {}
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return {}

    key = _cache_key(latitude, longitude)
    cache = _load_cache()
    if key in cache:
        cached = cache[key]
        return dict(cached) if isinstance(cached, dict) else {}

    params = urllib.parse.urlencode(
        {
            "lat": f"{latitude}",
            "lon": f"{longitude}",
            "format": "jsonv2",
            # zoom=18 is building/venue level. Lower returns a street or suburb,
            # which is too coarse to name the restaurant we are after.
            "zoom": "18",
            "addressdetails": "1",
        }
    )
    payload = _fetch_json(NOMINATIM_URL, dict(urllib.parse.parse_qsl(params)))

    summary: dict[str, Any] = {}
    if isinstance(payload, dict) and not payload.get("error"):
        summary = _summarize(payload)

    nearby = _nearby_places(latitude, longitude)
    if nearby:
        summary["nearby_places"] = "; ".join(
            f"{place['name']} ({place['category']}, {place['distance_m']}m)" for place in nearby
        )
        # The containing feature is frequently a parking lot or the retail park,
        # which is not somewhere you eat. A named venue a few metres away is the
        # better answer to "where was this taken".
        nearest = nearby[0]
        if not summary.get("place_name") and nearest["distance_m"] <= 50:
            summary["place_name"] = nearest["name"]
            summary["place_category"] = nearest["category"]

    # Cache negative results too, so a coordinate in the ocean is not retried on
    # every re-ingest.
    cache[key] = summary
    _store_cache(cache)
    return summary
