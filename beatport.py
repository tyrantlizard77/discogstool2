"""Beatport BPM lookup for Discogs releases.

Public API
----------
    matcher = BeatportMatcher()
    bpms = matcher.find_bpms(discogs_release)
    # bpms: dict mapping discogs track index (0-based) -> bpm (int)
    #       only includes tracks where a confident match was found

Configuration
-------------
Credentials are stored in ~/.discogstool/beatport_auth.json:
    {
        "username": "...",
        "password": "...",
        "access_token": "...",
        "refresh_token": "...",
        "expires_at": 1234567890.0
    }

Run interactively to set up credentials:
    python3 beatport.py --setup
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sqlite3
import time
import unicodedata
from abc import ABC, abstractmethod
from difflib import SequenceMatcher
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

import requests

import util

if TYPE_CHECKING:
    from client_interface import DiscogsRelease

log = logging.getLogger(__name__)

# ── API endpoints ──────────────────────────────────────────────────────────────

_API_BASE = "https://api.beatport.com/v4"
_HTTP_TIMEOUT = 30

# ── Regex patterns (from beets-beatport4) ─────────────────────────────────────

_SCRIPT_SRC_RE = re.compile(r"src=.(.*js)")
_CLIENT_ID_RE = re.compile(r"API_CLIENT_ID: '(.*)'")
_HTML_P_RE = re.compile(r"<p>(.*)</p>")

# ── Matching thresholds ────────────────────────────────────────────────────────

# Minimum SequenceMatcher ratio for catalog number similarity
CATNO_MIN_SCORE: float = 0.82

# Minimum title similarity (release-level)
TITLE_MIN_SCORE: float = 0.75

# Minimum title similarity when matching individual tracks
TRACK_MIN_SCORE: float = 0.72

# Minimum fraction of Discogs tracks that must match a Beatport track
TRACK_COVERAGE_MIN: float = 0.30

# Maximum year difference for a release to be considered a match
MAX_YEAR_DIFF: int = 1

# ── Cache TTLs (days) ──────────────────────────────────────────────────────────

RELEASE_CACHE_TTL_DAYS: int = 90
NOMATCH_TTL_DAYS: int = 30

# ── Auth config path ───────────────────────────────────────────────────────────

AUTH_FILE    = util.userfile("beatport_auth.json")
CACHE_FILE   = util.userfile("beatport.db")
_LOG_FILE    = util.userfile("beatport.log")


# ── File logger ───────────────────────────────────────────────────────────────

_file_handler_attached = False


def _attach_file_logger() -> None:
    """Attach a rotating file handler to the beatport module logger.

    Called on first BeatportMatcher instantiation.  All search queries,
    candidate scores, match decisions, and track-level BPM matches are
    written to ~/.discogstool/beatport.log at DEBUG level.

    The log rotates at 2 MB and keeps two backups (≤ 6 MB total).
    """
    global _file_handler_attached
    if _file_handler_attached:
        return
    _file_handler_attached = True

    handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=2 * 1024 * 1024,  # 2 MB per file
        backupCount=2,              # → .log  .log.1  .log.2  (max 6 MB)
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)


# ══════════════════════════════════════════════════════════════════════════════
# Normalisation helpers
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_title(title: str) -> str:
    """Normalize a track/release title for fuzzy comparison.

    - Lowercase
    - Remove accents (NFD decompose, strip Mn category)
    - Strip "Original Mix" / "Original" suffix
    - Remove feat./ft. credits
    - Collapse punctuation and whitespace
    """
    if not title:
        return ""
    # NFD → strip combining marks
    title = unicodedata.normalize("NFD", title)
    title = "".join(c for c in title if unicodedata.category(c) != "Mn")
    title = title.lower()
    # Remove feat./ft. credits in parens or after dash
    title = re.sub(r"\(feat\.?.*?\)", "", title, flags=re.I)
    title = re.sub(r"\bfeat\.?\s+\S+.*$", "", title, flags=re.I)
    # Remove "Original Mix" / "Original" at end or in parens
    title = re.sub(r"\(?original mix\)?", "", title, flags=re.I)
    title = re.sub(r"\boriginal\b", "", title, flags=re.I)
    # Collapse non-word chars
    title = re.sub(r"[^\w\s]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _normalize_catno(catno: str) -> str:
    """Normalize a catalog number: uppercase, remove spaces and hyphens."""
    return re.sub(r"[\s\-]", "", (catno or "").upper())


def _strip_discogs_artist(artist: str) -> str:
    """Strip Discogs disambiguation suffixes before using artist in searches.

    Discogs appends (2), (3), … to artist names when multiple artists share
    the same name.  Beatport has no such convention, so searching for
    "Artist (2)" would yield no results.  Handles multiple artists in one
    string, e.g. "Artist A (2) & Artist B (3)" → "Artist A & Artist B".
    """
    return re.sub(r"\s*\(\d+\)", "", artist).strip()


def _similarity(a: str, b: str) -> float:
    """Return SequenceMatcher ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _catno_similarity(a: str, b: str) -> float:
    return _similarity(_normalize_catno(a), _normalize_catno(b))


def _title_similarity(a: str, b: str) -> float:
    return _similarity(_normalize_title(a), _normalize_title(b))


# ══════════════════════════════════════════════════════════════════════════════
# Local cache (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

class BeatportCache:
    """Persistent cache for Beatport API responses and match decisions.

    Tables
    ------
    release_cache
        Raw Beatport release data (tracks included), keyed by Beatport release
        ID.  Expires after RELEASE_CACHE_TTL_DAYS days.

    matches
        Confirmed Discogs→Beatport release matches, with confidence score and
        the matcher that made the decision.  Permanent (never expires).

    nomatches
        Discogs release IDs for which no Beatport match was found.  Expires
        after NOMATCH_TTL_DAYS days so we retry later (e.g. after the release
        appears on Beatport).
    """

    def __init__(self, db_path: str = CACHE_FILE) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        c = self._conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS release_cache (
                beatport_id  TEXT PRIMARY KEY,
                fetched_date TEXT NOT NULL,
                data         TEXT NOT NULL  -- JSON
            );
            CREATE TABLE IF NOT EXISTS matches (
                discogs_id   TEXT PRIMARY KEY,
                beatport_id  TEXT NOT NULL,
                confidence   REAL NOT NULL,
                matcher      TEXT NOT NULL,
                matched_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nomatches (
                discogs_id   TEXT PRIMARY KEY,
                checked_date TEXT NOT NULL
            );
        """)
        self._conn.commit()

    @staticmethod
    def _today() -> str:
        import datetime
        return str(datetime.date.today())

    @staticmethod
    def _days_since(date_str: str) -> int:
        import datetime
        d = datetime.date.fromisoformat(date_str)
        return (datetime.date.today() - d).days

    # ── release cache ──────────────────────────────────────────────────────────

    def get_release(self, beatport_id: str) -> dict | None:
        """Return cached release data or None if absent/expired."""
        c = self._conn.cursor()
        c.execute(
            "SELECT fetched_date, data FROM release_cache WHERE beatport_id=?",
            (str(beatport_id),),
        )
        row = c.fetchone()
        if row is None:
            return None
        if self._days_since(row["fetched_date"]) > RELEASE_CACHE_TTL_DAYS:
            c.execute(
                "DELETE FROM release_cache WHERE beatport_id=?",
                (str(beatport_id),),
            )
            self._conn.commit()
            return None
        return json.loads(row["data"])

    def put_release(self, beatport_id: str, data: dict) -> None:
        c = self._conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO release_cache
               (beatport_id, fetched_date, data) VALUES (?,?,?)""",
            (str(beatport_id), self._today(), json.dumps(data)),
        )
        self._conn.commit()

    # ── match cache ────────────────────────────────────────────────────────────

    def get_match(self, discogs_id: str) -> tuple[str, float, str] | None:
        """Return (beatport_id, confidence, matcher) or None."""
        c = self._conn.cursor()
        c.execute(
            "SELECT beatport_id, confidence, matcher FROM matches WHERE discogs_id=?",
            (str(discogs_id),),
        )
        row = c.fetchone()
        if row is None:
            return None
        return (row["beatport_id"], row["confidence"], row["matcher"])

    def put_match(
        self,
        discogs_id: str,
        beatport_id: str,
        confidence: float,
        matcher: str,
    ) -> None:
        c = self._conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO matches
               (discogs_id, beatport_id, confidence, matcher, matched_date)
               VALUES (?,?,?,?,?)""",
            (str(discogs_id), str(beatport_id), confidence, matcher, self._today()),
        )
        self._conn.commit()

    # ── nomatch cache ──────────────────────────────────────────────────────────

    def is_known_nomatch(self, discogs_id: str) -> bool:
        """Return True if this Discogs ID was recently searched and not found."""
        c = self._conn.cursor()
        c.execute(
            "SELECT checked_date FROM nomatches WHERE discogs_id=?",
            (str(discogs_id),),
        )
        row = c.fetchone()
        if row is None:
            return False
        if self._days_since(row["checked_date"]) > NOMATCH_TTL_DAYS:
            c.execute(
                "DELETE FROM nomatches WHERE discogs_id=?",
                (str(discogs_id),),
            )
            self._conn.commit()
            return False
        return True

    def put_nomatch(self, discogs_id: str) -> None:
        c = self._conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO nomatches (discogs_id, checked_date) VALUES (?,?)",
            (str(discogs_id), self._today()),
        )
        self._conn.commit()

    def delete_nomatch(self, discogs_id: str) -> None:
        """Remove a nomatch entry (e.g. to force a retry)."""
        c = self._conn.cursor()
        c.execute("DELETE FROM nomatches WHERE discogs_id=?", (str(discogs_id),))
        self._conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP client
# ══════════════════════════════════════════════════════════════════════════════

class _BeatportClient:
    """Thin Beatport API v4 HTTP client.

    Authentication is handled externally; pass a valid access token.
    Token refresh is NOT handled here — see BeatportMatcher for that.
    """

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": "discogstool/1.0 +https://github.com/andrewboie/discogstool2",
        }

    def _make_url(self, endpoint: str, **params: object) -> str:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        url = _API_BASE + endpoint
        if params:
            url += "?" + urlencode(params)
        return url

    def get(self, endpoint: str, **params: object) -> dict | list:
        url = self._make_url(endpoint, **params)
        try:
            r = requests.get(url, headers=self._headers(), timeout=_HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise BeatportError(f"Network error: {e}") from e
        if not r.ok:
            raise BeatportError(
                f"HTTP {r.status_code} for {endpoint}",
                status_code=r.status_code,
            )
        try:
            data = r.json()
        except ValueError as e:
            raise BeatportError(f"Invalid JSON from {endpoint}: {e}") from e
        # Paginated endpoints wrap results
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def search_releases(self, query: str, per_page: int = 5) -> list[dict]:
        result = self.get("catalog/search", q=query, type="releases", per_page=per_page)
        if isinstance(result, dict):
            return result.get("releases", [])
        return []

    def get_release(self, beatport_id: int | str) -> dict:
        return self.get(f"/catalog/releases/{beatport_id}/")

    def get_release_tracks(self, beatport_id: int | str) -> list[dict]:
        result = self.get(
            f"/catalog/releases/{beatport_id}/tracks/",
            per_page=100,
        )
        return result if isinstance(result, list) else []

    def get_track(self, beatport_id: int | str) -> dict:
        return self.get(f"/catalog/tracks/{beatport_id}/")


class BeatportError(Exception):
    """Raised for all Beatport API errors (network, HTTP, auth, parse failures)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ══════════════════════════════════════════════════════════════════════════════
# OAuth2 authentication
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_client_id() -> str:
    """Scrape the Beatport API client ID from the docs page JS bundle."""
    try:
        html = requests.get(
            f"{_API_BASE}/docs/", timeout=_HTTP_TIMEOUT
        ).content.decode("utf-8")
    except requests.RequestException as e:
        raise BeatportError(f"Could not fetch docs page: {e}") from e

    for script_url in _SCRIPT_SRC_RE.findall(html):
        full_url = f"https://api.beatport.com{script_url}"
        try:
            js = requests.get(full_url, timeout=_HTTP_TIMEOUT).content.decode("utf-8")
        except requests.RequestException:
            continue
        matches = _CLIENT_ID_RE.findall(js)
        if matches:
            return matches[0]
    raise BeatportError("Could not scrape API_CLIENT_ID from Beatport docs page")


def _authorize(username: str, password: str, client_id: str) -> dict:
    """Run the Beatport OAuth2 authorization_code flow.

    Returns a dict with keys: access_token, refresh_token, expires_at.
    """
    redirect_uri = f"{_API_BASE}/auth/o/post-message/"
    try:
        with requests.Session() as s:
            # Step 1: Login with username/password to get session cookies
            r = s.post(
                f"{_API_BASE}/auth/login/",
                json={"username": username, "password": password},
                timeout=_HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            if "username" not in data or "email" not in data:
                raise BeatportError(f"Login failed: {data}")

            # Step 2: Get authorization code via OAuth2 redirect
            r = s.get(
                f"{_API_BASE}/auth/o/authorize/",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                },
                allow_redirects=False,
                timeout=_HTTP_TIMEOUT,
            )
            body = r.content.decode("utf-8")
            if "invalid_request" in body:
                paragraphs = _HTML_P_RE.findall(body)
                msg = paragraphs[0] if paragraphs else body
                raise BeatportError(f"OAuth error: {msg}")
            if "Location" not in r.headers:
                raise BeatportError(
                    f"OAuth redirect missing Location header (status={r.status_code})"
                )
            loc_raw = r.headers["Location"]
            # Location may be absolute (https://...) or relative (/path?code=...)
            if loc_raw.startswith("http://") or loc_raw.startswith("https://"):
                location = urlparse(loc_raw)
            else:
                location = urlparse(f"{_API_BASE}{loc_raw}")
            codes = parse_qs(location.query).get("code")
            if not codes:
                raise BeatportError("No authorization code in OAuth redirect")
            auth_code = codes[0]

            # Step 3: Exchange code for tokens
            r = s.post(
                f"{_API_BASE}/auth/o/token/",
                params={
                    "code": auth_code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                },
                timeout=_HTTP_TIMEOUT,
            )
            r.raise_for_status()
            token_data = r.json()
            expires_at = time.time() + int(token_data.get("expires_in", 3600))
            return {
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "expires_at": expires_at,
            }
    except requests.HTTPError as e:
        raise BeatportError(
            f"HTTP {e.response.status_code} during authorization"
        ) from e
    except requests.RequestException as e:
        raise BeatportError(f"Network error during authorization: {e}") from e


def _load_auth() -> dict | None:
    """Load auth config from disk. Returns None if file missing or malformed."""
    if not os.path.exists(AUTH_FILE):
        return None
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_auth(data: dict) -> None:
    with open(AUTH_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(AUTH_FILE, 0o600)


def get_client() -> _BeatportClient:
    """Return an authenticated Beatport client, refreshing the token if needed.

    Reads credentials from ~/.discogstool/beatport_auth.json.
    Raises BeatportError if not configured.
    """
    auth = _load_auth()
    if not auth:
        raise BeatportError(
            "Beatport credentials not configured. "
            "Run: python3 beatport.py --setup"
        )

    # Token still valid?
    expires_at = auth.get("expires_at", 0)
    if time.time() + 30 < expires_at and auth.get("access_token"):
        return _BeatportClient(auth["access_token"])

    # Token expired or missing — re-authorize
    username = auth.get("username")
    password = auth.get("password")
    if not username or not password:
        raise BeatportError(
            "Stored token expired and no username/password available. "
            "Run: python3 beatport.py --setup"
        )

    log.debug("Beatport token expired, re-authorizing...")
    client_id = _fetch_client_id()
    token = _authorize(username, password, client_id)
    auth.update(token)
    _save_auth(auth)
    log.debug("Beatport re-authorized successfully")
    return _BeatportClient(auth["access_token"])


# ══════════════════════════════════════════════════════════════════════════════
# Release matcher abstraction
# ══════════════════════════════════════════════════════════════════════════════

class ReleaseMatcher(ABC):
    """Abstract base class for Discogs→Beatport release matchers.

    Subclasses implement different matching strategies (catalog-number-based,
    LLM-assisted, etc.).  The BeatportMatcher orchestrator tries matchers in
    priority order and uses the first result that meets the confidence
    threshold.
    """

    #: Human-readable name used in cache records and log messages
    name: str = "unknown"

    @abstractmethod
    def find_release(
        self,
        discogs_release: "DiscogsRelease",
        client: _BeatportClient,
    ) -> tuple[str | None, float]:
        """Search for the Beatport release matching *discogs_release*.

        Returns
        -------
        (beatport_id, confidence)
            *beatport_id* is a string Beatport release ID, or ``None`` if no
            match was found.  *confidence* is a float in [0.0, 1.0].

        Notes
        -----
        Implementations should be conservative — false positives (returning
        the wrong release) are far worse than returning (None, 0.0).
        """


class CatnoMatcher(ReleaseMatcher):
    """Match releases by catalog number, with fuzzy title and year gating.

    Strategy
    --------
    1. Search Beatport for ``<catno> <artist>`` (and fallback to just ``<catno>``).
    2. For each result, compute catalog-number similarity and title similarity.
    3. Gate on:
       - catno similarity ≥ CATNO_MIN_SCORE, OR title similarity ≥ TITLE_MIN_SCORE
         with catno similarity ≥ 0.5 (partial match)
       - year within MAX_YEAR_DIFF (if both have years)
    4. Return the best candidate above threshold, or (None, 0.0).
    """

    name = "catno"

    def find_release(
        self,
        discogs_release: "DiscogsRelease",
        client: _BeatportClient,
    ) -> tuple[str | None, float]:
        catno = discogs_release.getCatno() or ""
        artist = _strip_discogs_artist(discogs_release.getArtist() or "")
        title = discogs_release.getTitle() or ""
        year = discogs_release.getYear()

        if not catno:
            log.debug("CatnoMatcher: no catalog number, skipping")
            return None, 0.0

        queries = [
            f"{catno} {artist}".strip(),  # primary: catno + artist
            catno,                         # fallback: catno alone
        ]

        best_id: str | None = None
        best_score: float = 0.0

        for query in queries:
            try:
                results = client.search_releases(query, per_page=10)
            except BeatportError as e:
                log.debug("CatnoMatcher: search error for %r: %s", query, e)
                continue

            for r in results:
                bp_catno = r.get("catalog_number") or ""
                bp_title = r.get("name") or ""
                bp_year = None
                if r.get("publish_date"):
                    try:
                        bp_year = int(str(r["publish_date"])[:4])
                    except (ValueError, TypeError):
                        pass

                c_sim = _catno_similarity(catno, bp_catno)
                t_sim = _title_similarity(title, bp_title)

                log.debug(
                    "CatnoMatcher: candidate [%s] catno=%r c_sim=%.2f t_sim=%.2f",
                    r.get("id"),
                    bp_catno,
                    c_sim,
                    t_sim,
                )

                # Year gate
                if year and bp_year and abs(int(year) - bp_year) > MAX_YEAR_DIFF:
                    log.debug(
                        "CatnoMatcher: year mismatch discogs=%s beatport=%s, skipping",
                        year,
                        bp_year,
                    )
                    continue

                # Score: catno similarity is primary, title is secondary
                if c_sim >= CATNO_MIN_SCORE:
                    score = c_sim * 0.7 + t_sim * 0.3
                elif c_sim >= 0.5 and t_sim >= TITLE_MIN_SCORE:
                    score = c_sim * 0.4 + t_sim * 0.6
                else:
                    continue  # not a viable candidate

                if score > best_score:
                    best_score = score
                    best_id = str(r["id"])

            if best_id:
                break  # don't try fallback query if first worked

        if best_id:
            log.debug(
                "CatnoMatcher: best match beatport_id=%s score=%.3f",
                best_id,
                best_score,
            )
        else:
            log.debug("CatnoMatcher: no match found for catno=%r", catno)

        return best_id, best_score


class TitleMatcher(ReleaseMatcher):
    """Match releases by title + artist, for when catno-based search fails.

    Beatport's search index sometimes doesn't surface a release by catalog
    number (e.g. for newer or smaller labels), but finds it immediately by
    release title.  The same catno + title scoring gates as CatnoMatcher apply
    — we still require a strong catalog-number match on the result, so a
    coincidentally same-titled release on a different label won't be accepted.
    """

    name = "title"

    def find_release(
        self,
        discogs_release: "DiscogsRelease",
        client: _BeatportClient,
    ) -> tuple[str | None, float]:
        title  = discogs_release.getTitle()  or ""
        artist = _strip_discogs_artist(discogs_release.getArtist() or "")
        catno  = discogs_release.getCatno()  or ""
        year   = discogs_release.getYear()

        if not title:
            return None, 0.0

        queries = [
            f"{title} {artist}".strip(),
            title,
        ]

        best_id: str | None = None
        best_score: float   = 0.0

        for query in queries:
            try:
                results = client.search_releases(query, per_page=10)
            except BeatportError as e:
                log.debug("TitleMatcher: search error for %r: %s", query, e)
                continue

            for r in results:
                bp_catno = r.get("catalog_number") or ""
                bp_title = r.get("name")            or ""
                bp_year  = None
                if r.get("publish_date"):
                    try:
                        bp_year = int(str(r["publish_date"])[:4])
                    except (ValueError, TypeError):
                        pass

                c_sim = _catno_similarity(catno, bp_catno)
                t_sim = _title_similarity(title, bp_title)

                log.debug(
                    "TitleMatcher: candidate [%s] catno=%r c_sim=%.2f t_sim=%.2f",
                    r.get("id"), bp_catno, c_sim, t_sim,
                )

                if year and bp_year and abs(int(year) - bp_year) > MAX_YEAR_DIFF:
                    log.debug(
                        "TitleMatcher: year mismatch discogs=%s beatport=%s, skipping",
                        year, bp_year,
                    )
                    continue

                if c_sim >= CATNO_MIN_SCORE:
                    score = c_sim * 0.7 + t_sim * 0.3
                elif c_sim >= 0.5 and t_sim >= TITLE_MIN_SCORE:
                    score = c_sim * 0.4 + t_sim * 0.6
                else:
                    continue

                if score > best_score:
                    best_score = score
                    best_id    = str(r["id"])

            if best_id:
                break

        if best_id:
            log.debug(
                "TitleMatcher: best match beatport_id=%s score=%.3f",
                best_id, best_score,
            )
        else:
            log.debug("TitleMatcher: no match found for title=%r", title)

        return best_id, best_score


class LLMMatcher(ReleaseMatcher):
    """LLM-assisted release matcher using a remote local LLM server.

    This matcher sends release metadata to a locally-running LLM (e.g. on a
    Mac Studio M1 Ultra with 512GB RAM) and asks it to identify the Beatport
    release.  It is intended for cases where catalog-number matching fails
    (different pressings, regional variants, etc.).

    Configuration
    -------------
    The LLM endpoint URL is read from beatport_auth.json under the key
    ``"llm_url"``.  For example:

        "llm_url": "http://mac-studio.local:11434/api/generate"

    The LLM is expected to follow an Ollama-compatible API:
        POST /api/generate
        {"model": "...", "prompt": "...", "stream": false}

    The prompt asks the LLM to return a JSON object:
        {"beatport_id": "12345678", "confidence": 0.92}
    or  {"beatport_id": null, "confidence": 0.0}

    Status
    ------
    Not yet implemented.  Returns (None, 0.0) until configured and tested.
    """

    name = "llm"

    #: Minimum confidence score accepted from the LLM
    MIN_LLM_CONFIDENCE: float = 0.85

    def __init__(self) -> None:
        auth = _load_auth() or {}
        self._llm_url: str | None = auth.get("llm_url")
        self._llm_model: str = auth.get("llm_model", "llama3")

    def is_available(self) -> bool:
        return bool(self._llm_url)

    def find_release(
        self,
        discogs_release: "DiscogsRelease",
        client: _BeatportClient,
    ) -> tuple[str | None, float]:
        if not self._llm_url:
            log.debug("LLMMatcher: no llm_url configured, skipping")
            return None, 0.0

        prompt = self._build_prompt(discogs_release)
        log.debug("LLMMatcher: querying %s", self._llm_url)

        try:
            r = requests.post(
                self._llm_url,
                json={"model": self._llm_model, "prompt": prompt, "stream": False},
                timeout=60,
            )
            r.raise_for_status()
            response_text = r.json().get("response", "")
        except requests.RequestException as e:
            log.warning("LLMMatcher: request failed: %s", e)
            return None, 0.0

        result = self._parse_llm_response(response_text)
        if result is None:
            log.debug("LLMMatcher: could not parse LLM response")
            return None, 0.0

        beatport_id, confidence = result
        if confidence < self.MIN_LLM_CONFIDENCE:
            log.debug(
                "LLMMatcher: confidence %.2f below threshold %.2f",
                confidence,
                self.MIN_LLM_CONFIDENCE,
            )
            return None, 0.0

        log.debug("LLMMatcher: beatport_id=%s confidence=%.2f", beatport_id, confidence)
        return beatport_id, confidence

    def _build_prompt(self, discogs_release: "DiscogsRelease") -> str:
        """Build the prompt sent to the LLM."""
        tracks = []
        for i in range(100):
            try:
                t = discogs_release.getTrack(i)
                tracks.append(f"  {t.getPosition()} {t.getTitle()}")
            except (IndexError, AttributeError):
                break

        track_list = "\n".join(tracks[:20]) if tracks else "  (none)"
        return f"""You are a music database assistant. Given a vinyl release from Discogs, find the matching release on Beatport.

Discogs release:
  Artist: {discogs_release.getArtist()}
  Title:  {discogs_release.getTitle()}
  Label:  {discogs_release.getLabel()}
  Catno:  {discogs_release.getCatno()}
  Year:   {discogs_release.getYear()}
  Country: {discogs_release.getCountry()}
  Tracks:
{track_list}

Search Beatport for this release. If you find a confident match, respond with ONLY a JSON object:
{{"beatport_id": "12345678", "confidence": 0.92}}

If you cannot find a match or are not confident, respond with:
{{"beatport_id": null, "confidence": 0.0}}

Important: false positives are very bad. Only return a beatport_id if you are highly confident.
Respond with ONLY the JSON object, no other text."""

    @staticmethod
    def _parse_llm_response(text: str) -> tuple[str | None, float] | None:
        """Extract (beatport_id, confidence) from LLM JSON response."""
        # Find JSON object in the response (LLM may include extra text)
        match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
            bid = data.get("beatport_id")
            conf = float(data.get("confidence", 0.0))
            if bid is None:
                return None, 0.0
            return str(bid), conf
        except (json.JSONDecodeError, TypeError, ValueError):
            return None


# ══════════════════════════════════════════════════════════════════════════════
# Track BPM resolution
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_full_release(
    beatport_id: str,
    client: _BeatportClient,
    cache: BeatportCache,
) -> dict | None:
    """Fetch a Beatport release with its full tracklist, using cache."""
    cached = cache.get_release(beatport_id)
    if cached is not None:
        log.debug("Using cached Beatport release %s", beatport_id)
        return cached

    log.debug("Fetching Beatport release %s", beatport_id)
    try:
        release = client.get_release(beatport_id)
    except BeatportError as e:
        log.warning("Could not fetch Beatport release %s: %s", beatport_id, e)
        return None

    # Fetch track list for this release.
    try:
        track_stubs = client.get_release_tracks(beatport_id)
    except BeatportError as e:
        log.warning("Could not fetch tracks for release %s: %s", beatport_id, e)
        track_stubs = []

    # Beatport track stubs usually include 'bpm'.  Only fetch the full track
    # object when 'bpm' is absent from the stub, to avoid N extra API round
    # trips for the common case.
    full_tracks = []
    for stub in track_stubs:
        if stub is None:
            continue
        tid = stub.get("id")
        if not tid:
            continue
        if stub.get("bpm") is not None:
            full_tracks.append(stub)
            continue
        # bpm not in stub — fetch full track detail
        try:
            full_track = client.get_track(tid)
            full_tracks.append(full_track)
        except BeatportError as e:
            log.debug("Could not fetch full track %s: %s", tid, e)
            full_tracks.append(stub)  # use stub as fallback (bpm will be None)

    release["tracks"] = full_tracks
    cache.put_release(beatport_id, release)
    return release


def _match_tracks(
    discogs_release: "DiscogsRelease",
    beatport_tracks: list[dict],
) -> dict[int, dict]:
    """Fuzzy-match Discogs tracks to Beatport tracks by title.

    Returns a dict: {discogs_track_index (0-based): {"bpm": int|None, "duration_ms": int|None}}.
    Includes an entry whenever title similarity >= TRACK_MIN_SCORE and at least
    one of bpm or duration_ms is available.

    Digital releases on Beatport may have different track orders and
    exclusive tracks compared to the vinyl release on Discogs.
    We match by title similarity rather than position.
    """
    result: dict[int, dict] = {}

    # Build normalized Beatport track list once
    bp_normalized = []
    for bt in beatport_tracks:
        if bt is None:
            continue
        name = bt.get("name") or ""
        mix = bt.get("mix_name") or ""
        # Combine name + mix_name into a single title string
        if mix and mix.lower() not in ("original mix", "original"):
            combined = f"{name} ({mix})"
        else:
            combined = name
        bpm       = bt.get("bpm")
        length_ms = bt.get("length_ms")
        bp_normalized.append((_normalize_title(combined), bpm, length_ms))

    # Collect Discogs tracks
    discogs_tracks = []
    for i in range(200):
        try:
            t = discogs_release.getTrack(i)
            if t is None:
                break
            discogs_tracks.append((i, t))
        except Exception:
            break

    # title_matched counts ALL discogs tracks whose title matched a Beatport track
    # (regardless of whether BPM/duration was available).  This is what we use for
    # the coverage gate — we want to detect a wrong release, not punish releases
    # where Beatport simply hasn't filled in BPM values.
    title_matched_count = 0

    for idx, dt in discogs_tracks:
        dt_title = _normalize_title(dt.getTitle() or "")
        if not dt_title:
            continue

        best_score:     float    = 0.0
        best_bpm:       int | None = None
        best_length_ms: int | None = None

        for bp_title, bp_bpm, bp_length_ms in bp_normalized:
            score = _similarity(dt_title, bp_title)
            if score > best_score:
                best_score     = score
                best_bpm       = bp_bpm
                best_length_ms = bp_length_ms

        if best_score >= TRACK_MIN_SCORE:
            title_matched_count += 1
            if best_bpm is not None or best_length_ms is not None:
                result[idx] = {
                    "bpm":         int(best_bpm) if best_bpm is not None else None,
                    "duration_ms": int(best_length_ms) if best_length_ms is not None else None,
                }
                log.debug(
                    "Track match: [%d] %r → bpm=%s dur_ms=%s (score=%.2f)",
                    idx,
                    dt.getTitle(),
                    result[idx]["bpm"],
                    result[idx]["duration_ms"],
                    best_score,
                )
            else:
                log.debug(
                    "Track match: [%d] %r → no BPM/duration on Beatport (score=%.2f)",
                    idx,
                    dt.getTitle(),
                    best_score,
                )

    # Coverage gate: reject if too few Discogs tracks title-matched a Beatport
    # track.  This catches a wrong release match, not missing BPM data.
    if discogs_tracks:
        coverage = title_matched_count / len(discogs_tracks)
        if coverage < TRACK_COVERAGE_MIN and len(discogs_tracks) >= 4:
            log.warning(
                "Track title coverage too low (%.0f%% < %.0f%%), "
                "release match probably wrong — discarding BPM data",
                coverage * 100,
                TRACK_COVERAGE_MIN * 100,
            )
            return {}

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class BeatportMatcher:
    """Orchestrates Beatport BPM lookups for Discogs releases.

    Usage
    -----
        matcher = BeatportMatcher()
        bpms = matcher.find_bpms(discogs_release)
        # bpms: {discogs_track_index: bpm_int} for tracks with known BPM

    Matcher priority
    ----------------
    Matchers are tried in order.  The first result meeting its confidence
    threshold is used.  To add a new matching strategy, subclass
    ReleaseMatcher and pass it in the *matchers* list.

    False positive protection
    -------------------------
    Even after a confident release match, the track-level coverage gate
    ensures the match makes sense (enough Discogs tracks found on Beatport).
    """

    #: Minimum confidence from any matcher to accept a release match
    MIN_RELEASE_CONFIDENCE: float = 0.70

    def __init__(
        self,
        matchers: list[ReleaseMatcher] | None = None,
        cache: BeatportCache | None = None,
    ) -> None:
        if matchers is None:
            matchers = [CatnoMatcher(), TitleMatcher(), LLMMatcher()]
        self._matchers = matchers
        self._cache = cache or BeatportCache()
        _attach_file_logger()

    def find_bpms(
        self,
        discogs_release: "DiscogsRelease",
        force: bool = False,
    ) -> dict[int, dict]:
        """Find BPMs and durations for all tracks in a Discogs release.

        Parameters
        ----------
        discogs_release:
            A DiscogsRelease instance.
        force:
            If True, ignore cached nomatch entries and retry.

        Returns
        -------
        dict mapping 0-based Discogs track index →
            {"bpm": int|None, "duration_ms": int|None}.
        An empty dict means no match was found or no data is available.
        """
        discogs_id = str(discogs_release.getId())

        log.debug(
            "=== lookup r%s  %s — %s  [%s]  %s ===",
            discogs_id,
            discogs_release.getArtist(),
            discogs_release.getTitle(),
            discogs_release.getCatno(),
            discogs_release.getYear(),
        )

        # Check cached nomatch
        if not force and self._cache.is_known_nomatch(discogs_id):
            log.debug("Discogs release %s is a cached nomatch, skipping", discogs_id)
            return {}

        # We need an authenticated client for both matching and BPM resolution.
        # Get it once here and pass it through to avoid redundant auth file reads.
        try:
            client = get_client()
        except BeatportError as e:
            log.warning("Cannot get Beatport client: %s", e)
            return {}

        # Check cached match — still need client for BPM resolution
        cached_match = self._cache.get_match(discogs_id)
        if cached_match:
            beatport_id, confidence, matcher_name = cached_match
            log.debug(
                "Using cached match: discogs=%s → beatport=%s (conf=%.2f, via %s)",
                discogs_id,
                beatport_id,
                confidence,
                matcher_name,
            )
            return self._resolve_bpms(discogs_release, beatport_id, client)

        # Try each matcher in order
        beatport_id: str | None = None
        best_confidence: float = 0.0
        winning_matcher: str = "none"

        for m in self._matchers:
            try:
                bid, confidence = m.find_release(discogs_release, client)
            except Exception as e:
                log.warning("Matcher %s raised: %s", m.name, e)
                continue

            if bid and confidence >= self.MIN_RELEASE_CONFIDENCE:
                if confidence > best_confidence:
                    beatport_id = bid
                    best_confidence = confidence
                    winning_matcher = m.name
                    # Keep trying others for potentially better confidence
            # If first matcher succeeds, we could break early for speed:
            # break

        if beatport_id is None:
            log.info(
                "No Beatport match found for Discogs release %s (%s - %s)",
                discogs_id,
                discogs_release.getArtist(),
                discogs_release.getTitle(),
            )
            self._cache.put_nomatch(discogs_id)
            return {}

        log.info(
            "Matched Discogs %s → Beatport %s (conf=%.2f, via %s)",
            discogs_id,
            beatport_id,
            best_confidence,
            winning_matcher,
        )
        self._cache.put_match(discogs_id, beatport_id, best_confidence, winning_matcher)
        return self._resolve_bpms(discogs_release, beatport_id, client)

    def _resolve_bpms(
        self,
        discogs_release: "DiscogsRelease",
        beatport_id: str,
        client: _BeatportClient | None = None,
    ) -> dict[int, dict]:
        """Fetch Beatport tracks and fuzzy-match to Discogs tracks."""
        if client is None:
            try:
                client = get_client()
            except BeatportError as e:
                log.warning("Cannot get Beatport client for BPM resolution: %s", e)
                return {}

        release_data = _fetch_full_release(beatport_id, client, self._cache)
        if release_data is None:
            return {}

        beatport_tracks = release_data.get("tracks", [])
        if not beatport_tracks:
            log.debug("No tracks found in Beatport release %s", beatport_id)
            return {}

        return _match_tracks(discogs_release, beatport_tracks)


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _setup_credentials() -> None:
    """Interactive setup to store Beatport credentials."""
    print("Beatport credential setup")
    print("Credentials are stored in:", AUTH_FILE)
    print()

    auth = _load_auth() or {}
    username = input(f"Beatport username [{auth.get('username', '')}]: ").strip()
    if username:
        auth["username"] = username
    password = input("Beatport password (leave blank to keep existing): ").strip()
    if password:
        auth["password"] = password

    llm_url = input(
        f"LLM server URL for LLMMatcher [{auth.get('llm_url', '')}] "
        "(leave blank to skip): "
    ).strip()
    if llm_url:
        auth["llm_url"] = llm_url
        llm_model = input(
            f"LLM model name [{auth.get('llm_model', 'llama3')}]: "
        ).strip()
        if llm_model:
            auth["llm_model"] = llm_model

    # Clear cached token so it gets refreshed
    auth.pop("access_token", None)
    auth.pop("expires_at", None)

    _save_auth(auth)
    print("Saved. Testing connection...")

    try:
        client = get_client()
        results = client.search_releases("test", per_page=1)
        print(f"Connection OK (got {len(results)} result(s))")
    except BeatportError as e:
        print(f"Connection failed: {e}")
        return

    print("Setup complete.")


if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    parser = argparse.ArgumentParser(
        description="Beatport BPM lookup for Discogs releases"
    )
    parser.add_argument("--setup", action="store_true", help="Set up credentials")
    parser.add_argument(
        "--release",
        metavar="DISCOGS_ID",
        help="Look up BPMs for a Discogs release (requires discogstool env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cached nomatch, force re-query",
    )
    parser.add_argument(
        "--clear-match",
        metavar="DISCOGS_ID",
        help="Remove cached match for a Discogs release",
    )
    args = parser.parse_args()

    if args.setup:
        _setup_credentials()
        sys.exit(0)

    if args.clear_match:
        cache = BeatportCache()
        cache.delete_nomatch(args.clear_match)
        c = cache._conn.cursor()
        c.execute("DELETE FROM matches WHERE discogs_id=?", (args.clear_match,))
        cache._conn.commit()
        print(f"Cleared cached match for Discogs release {args.clear_match}")
        sys.exit(0)

    if args.release:
        # Import here to avoid circular dependency during module load
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from client_interface import DiscogsRelease, DiscogsClient

        dc = DiscogsClient()
        dr = dc.getRelease(int(args.release))
        matcher = BeatportMatcher()
        bpms = matcher.find_bpms(dr, force=args.force)
        if bpms:
            print(f"\nBeatport data for Discogs release {args.release}:")
            for i in range(200):
                try:
                    t = dr.getTrack(i)
                    if t is None:
                        break
                    info    = bpms.get(i) or {}
                    bpm_str = str(info["bpm"]) if info.get("bpm") else "-"
                    ms      = info.get("duration_ms")
                    dur_str = f"{ms//1000//60}:{ms//1000%60:02d}" if ms else "-"
                    print(f"  [{i:3d}] {t.getPosition():4s} {t.getTitle():<50s}  bpm={bpm_str}  dur={dur_str}")
                except (IndexError, AttributeError):
                    break
        else:
            print(f"No BPMs found for Discogs release {args.release}")
        sys.exit(0)

    parser.print_help()
