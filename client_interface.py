#!/usr/bin/env python3

from __future__ import annotations

import discogs_client
import util
import os
import pprint
import re
import database
import time
import urllib.request, urllib.error, urllib.parse
import sys
import multiprocessing
import threading
import hashlib
import io
from typing import TypeAlias, TypedDict, cast
from PIL import Image

discogs_auth = util.userfile("discogs_auth")
useragent = "discogstool/2.0"
consumer_key = "mWCofNBrngwtGCSBOTDe"
consumer_secret = "nBgWYPSMtAonLobnAuZiowpJyUzhbcgW"
cached_instance: discogs_client.Client | None = None

url_headers: dict[str, str] = {
    'User-Agent' : useragent
}

discogs_lock = multiprocessing.Lock()


# ── TypedDicts for Discogs API response structures ─────────────────────────────

class DiscogsArtistRef(TypedDict, total=False):
    name: str
    anv: str
    id: int


class DiscogsExtraArtistRef(TypedDict, total=False):
    """One entry of an `extraartists` list — a credit rather than a performer.

    `role` may pack several roles ("Artwork, Design") and bracketed qualifiers
    ("Remix [Reshaped]").  On a release-level entry, `tracks` scopes the credit
    to particular tracks; empty means the whole release.
    """
    name: str
    anv: str
    id: int
    role: str
    tracks: str


class DiscogsCredit(TypedDict):
    """One role held by one artist on one track, as resolved by getCredits()."""
    role: str        # a single role, qualifier included: "Producer [Additional]"
    name: str        # as credited on the release (ANV if set), " (2)" kept
    artist_id: int   # 0 when Discogs has no linked artist


class DiscogsLabelRef(TypedDict, total=False):
    name: str
    catno: str


class _DiscogsImageRefRequired(TypedDict):
    uri: str


class DiscogsImageRef(_DiscogsImageRefRequired, total=False):
    type: str
    width: int
    height: int


class _DiscogsTrackDataRequired(TypedDict):
    position: str
    title: str
    type_: str


class DiscogsTrackData(_DiscogsTrackDataRequired, total=False):
    artists: list[DiscogsArtistRef]
    extraartists: list[DiscogsExtraArtistRef]
    duration: str


class _DiscogsReleaseDataRequired(TypedDict):
    tracklist: list[DiscogsTrackData]
    title: str
    artists: list[DiscogsArtistRef]
    labels: list[DiscogsLabelRef]


class DiscogsReleaseData(_DiscogsReleaseDataRequired, total=False):
    year: int
    master_id: int
    images: list[DiscogsImageRef]
    country: str
    styles: list[str]
    genres: list[str]
    extraartists: list[DiscogsExtraArtistRef]


class ClientException(Exception):
    pass


def get_user_auth_tokens() -> tuple[str, str] | tuple[None, None]:
    if os.path.exists(discogs_auth):
        try:
            with open(discogs_auth, "r") as fp:
                token, secret = fp.read().split("|")
        except ValueError:
            # File exists but content is malformed (e.g. missing '|' separator).
            # Delete it so the next run triggers a fresh OAuth flow.
            os.unlink(discogs_auth)
            return None, None
        except OSError:
            # Transient I/O error — don't delete the file, just return no tokens.
            return None, None
        return str(token), str(secret)
    else:
        return None, None


def set_user_auth_tokens(token: str, secret: str) -> None:
    # Open with O_CREAT|O_WRONLY and mode 0o600 so the file is never
    # world-readable, even transiently.  This avoids a race between write
    # and a subsequent chmod call.
    fd = os.open(discogs_auth, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        fp.write("%s|%s" % (token, secret))


def get_client_instance() -> discogs_client.Client:
    global cached_instance
    if cached_instance:
        return cached_instance

    token, secret = get_user_auth_tokens()

    if not token:
        c = discogs_client.Client(useragent, consumer_key, consumer_secret)

        access_token, access_secret, authorize_url = c.get_authorize_url()
        print(authorize_url)

        verifier = input("Verification code: ")

        token, secret = c.get_access_token(verifier)
        set_user_auth_tokens(token, secret)
    else:
        c = discogs_client.Client(useragent, consumer_key, consumer_secret,
                                token, secret)
    cached_instance = c
    return c


# Recursive type alias for arbitrary JSON-like values (Python 3.10+)
JsonValue: TypeAlias = "str | int | float | bool | None | dict[str, JsonValue] | list[JsonValue]"


def scrub_data(data: JsonValue) -> JsonValue:
    """Recursively strip whitespace from all string values in a JSON-like structure."""
    if isinstance(data, dict):
        return {k: scrub_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub_data(i) for i in data]
    if isinstance(data, str):
        return data.strip()
    return data


threadlocal = threading.local()


def _normalize_artwork(imgdata: bytes) -> bytes:
    """Return artwork as a CDJ-compatible JPEG, resized to fit within 800×800.

    Pioneer CDJs require embedded artwork to be JPEG format and no larger than
    800×800 pixels.  This normalises whatever Discogs serves (PNG, large JPEG,
    etc.) into a spec-compliant image so tagging always works correctly.

    The on-disk cache retains the original bytes; only the in-memory copy
    returned by getArtwork() is normalised.
    """
    img = Image.open(io.BytesIO(imgdata))
    if img.width > 800 or img.height > 800:
        img.thumbnail((800, 800), Image.LANCZOS)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _text(value: object) -> str:
    """A Discogs field as display text, with "no information" as "".

    The API sends null for fields it has nothing for — `country` is null on
    about 1% of releases — and a key holding null defeats `.get(key, "")`,
    so the old accessors handed None to f-strings and labels read "None".
    """
    return "" if value is None else str(value).strip()


def split_roles(role: str) -> list[str]:
    """Split a Discogs role string into its individual roles.

    Discogs packs several roles into one string ("Artwork, Design"), and a
    role's bracketed qualifier can itself contain commas ("Remix [Remixed,
    Reshaped]"), so this splits only on commas outside brackets.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in role:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    return [p for p in parts if p]


def _credits_from(entry: DiscogsExtraArtistRef) -> list[DiscogsCredit]:
    """Expand one extraartists entry into one DiscogsCredit per role."""
    name = _text(entry.get("anv")) or _text(entry.get("name"))
    if not name:
        return []
    artist_id = entry.get("id") or 0
    return [{"role": role, "name": name, "artist_id": artist_id}
            for role in split_roles(_text(entry.get("role")))]


# Separators and range syntax seen in the `tracks` field of release credits:
# "A1, B1", "C2 & C3", "A1 to A3", "A to B2" (a bare side as an endpoint).
_SCOPE_SPLIT_RE = re.compile(r"\s*[,&]\s*")
_SCOPE_RANGE_RE = re.compile(r"^(.+?)\s+to\s+(.+)$", re.IGNORECASE)
# "All", "All Music", "All except D1", "All tracks except A1, A2".  The
# exception list may follow other words, so look for "except" anywhere.
_SCOPE_ALL_RE   = re.compile(r"^all\b(?:.*?\bexcept\b\s*(.*))?", re.IGNORECASE)
_SIDE_RE        = re.compile(r"^([A-Za-z]+)")


class DiscogsRelease:
    data: DiscogsReleaseData
    rid: int
    totaltracks: int
    imgdata: bytes | None

    def __getitem__(self, key: str) -> DiscogsTrackData | DiscogsArtistRef | DiscogsLabelRef | DiscogsImageRef | list[DiscogsTrackData] | list[DiscogsArtistRef] | list[DiscogsLabelRef] | list[DiscogsImageRef] | list[str] | str | int:
        return self.data[key]  # type: ignore[literal-required]

    def getData(self, rid: int) -> DiscogsReleaseData:
        db = getattr(threadlocal, "db", None)
        if db is None:
            db = database.DiscogsDatabase()
            threadlocal.db = db

        key = "release-%d" % rid
        raw = db.get(key)
        if raw is not None:
            data = cast(DiscogsReleaseData, raw)
            if "tracklist" in data:
                return data
            else:
                db.delete(key)

        with discogs_lock:
            client = get_client_instance()

            # Even though rate limiting properly, still see transient
            # "Connection Reset By Peer" and "Bad Status Line" errors
            release = None
            for i in range(3):
                try:
                    time.sleep(1.1 + (5 * i))
                    release = client.release(rid)
                    release.refresh()
                    break
                except Exception:
                    continue

            if release is None:
                raise ClientException("release %d couldn't be fetched" % rid)

        data = cast(DiscogsReleaseData, scrub_data(cast(DiscogsReleaseData, release.data)))  # type: ignore[union-attr]
        db.put(key, data)

        return data

    def __init__(self, rid: int) -> None:
        self.rid = rid
        self.data = self.getData(rid)

        # Filter out non-track items in the tracklist like headings
        self.data["tracklist"] = [
            i for i in self.data["tracklist"]
            if i["type_"] == "track" and i["title"] != ""
        ]
        self.totaltracks = len(self.data["tracklist"])
        self.imgdata = None

    def isCompilation(self) -> bool:
        art = self.getTrack(0).getArtist()

        for i in range(1, self.getTotalTracks()):
            if self.getTrack(i).getArtist() != art:
                return True

        return False

    def getId(self) -> int:
        return self.rid

    def getTotalTracks(self) -> int:
        return self.totaltracks

    def getTrack(self, index: int) -> DiscogsTrack:
        return DiscogsTrack(self, index)

    def getMasterYear(self) -> str | None:
        """Return the earliest release year from the master release, or None.

        Discogs master releases carry the year of the first-ever pressing.
        Returns None when this release has no master, the master cannot be
        fetched, or the master has no year set.
        """
        master_id = self.data.get("master_id")
        if not master_id:  # absent, None, or 0 (Discogs uses 0 for "no master")
            return None

        db = getattr(threadlocal, "db", None)
        if db is None:
            db = database.DiscogsDatabase()
            threadlocal.db = db

        key = f"master-{master_id}"
        cached = db.get(key)
        if cached is not None:
            year = cached.get("year")
            return str(year) if year else None

        with discogs_lock:
            client = get_client_instance()
            master = None
            for i in range(3):
                try:
                    time.sleep(1.1 + (5 * i))
                    master = client.master(master_id)
                    master.refresh()
                    break
                except Exception:
                    continue

        if master is None:
            return None

        master_data = cast(dict, scrub_data(master.data))
        db.put(key, master_data)
        year = master_data.get("year")
        return str(year) if year else None

    def getYear(self) -> str:
        """Return the pressing year as a plain string (safe for file tagging).

        Blank when Discogs has no year: null, missing, or 0 — its placeholder
        for "unknown".  "0" used to come back, which tagged files with year
        0000 and made Beatport's year gate (`if year and bp_year`) reject
        every candidate, since "0" is truthy and ~2000 years off.
        """
        year = self.data.get("year")
        return _text(year) if year else ""

    def getLabelYear(self) -> str:
        """Return the year string formatted for sleeve labels.

        Shows the original release year from the master, with the pressing
        year appended in parentheses when this is a later repress:
          - First pressing (or no master data): "2004"
          - Repress: "2004 (2025)"
        """
        pressing_year = self.data.get("year") or 0
        master_year = self.getMasterYear()

        if master_year and pressing_year and master_year != str(pressing_year):
            return f"{pressing_year} ({master_year})"
        if pressing_year:
            return str(pressing_year)
        if master_year:
            return master_year
        return ""

    def compileListData(self, listname: str, keys: list[str]) -> str:
        # Discogs can return an empty list — r3060397 has no labels at all —
        # and indexing items[0] used to crash getLabel()/getCatno() outright.
        items = cast(list[dict[str, str]], self.data.get(listname) or [])  # type: ignore[misc]
        # Each item contributes its first non-blank key; items with nothing
        # (null or empty, e.g. a second label with no catno) are skipped
        # rather than joined as "None" or a dangling " / ".
        values = []
        for item in items:
            for key in keys:
                value = _text(item.get(key))
                if value:
                    values.append(value)
                    break
        return " / ".join(values)

    def getArtist(self) -> str:
        return self.compileListData("artists", ["anv", "name"])

    def getArtistIds(self) -> set[int]:
        """Discogs ids of the release's main artists (unlinked ones omitted)."""
        return {a["id"] for a in self.data.get("artists", []) if a.get("id")}

    def resolveTrackScope(self, spec: str) -> set[int] | None:
        """Map a release credit's `tracks` field to 0-based track indices.

        Returns None for an empty field: the credit applies to the whole
        release.  Otherwise returns the set of covered indices, matching
        getTrack(i).  Understands everything observed across a 1000-release
        collection:

          "A1", "A1, B1", "C2 & C3"   explicit positions
          "A1 to A3"                  inclusive range, in tracklist order
          "A", "B"                    a bare side: every track on it
          "A to B2"                   a side as a range endpoint
          "All Music", "All except D1"

        Tokens that match no position are ignored rather than guessed at —
        "C3 outro" contributes nothing, so a credit is never attached to a
        track it can't be tied to.
        """
        spec = (spec or "").strip()
        if not spec:
            return None

        positions = [_text(t.get("position")).upper()
                     for t in self.data["tracklist"]]
        everything = set(range(len(positions)))

        m = _SCOPE_ALL_RE.match(spec)
        if m:
            excluded = m.group(1)
            return everything - self._scopeTokens(excluded, positions) if excluded else everything

        return self._scopeTokens(spec, positions)

    @staticmethod
    def _scopeTokens(text: str, positions: list[str]) -> set[int]:
        """Resolve a comma/ampersand separated list of positions, sides and ranges."""

        def side_of(pos: str) -> str:
            m = _SIDE_RE.match(pos)
            return m.group(1) if m else ""

        def on_side(side: str) -> list[int]:
            return [i for i, p in enumerate(positions) if side_of(p) == side]

        def endpoint(tok: str, first: bool) -> int | None:
            tok = tok.strip().upper()
            if tok in positions:
                return positions.index(tok) if first else len(positions) - 1 - positions[::-1].index(tok)
            if tok.isalpha():                     # a bare side, e.g. "A to B2"
                hits = on_side(tok)
                if hits:
                    return hits[0] if first else hits[-1]
            return None

        out: set[int] = set()
        for tok in _SCOPE_SPLIT_RE.split(text.strip()):
            if not tok:
                continue
            rng = _SCOPE_RANGE_RE.match(tok)
            if rng:
                lo = endpoint(rng.group(1), first=True)
                hi = endpoint(rng.group(2), first=False)
                if lo is not None and hi is not None and lo <= hi:
                    out.update(range(lo, hi + 1))
                continue
            up = tok.strip().upper()
            if up in positions:
                out.update(i for i, p in enumerate(positions) if p == up)
            elif up.isalpha():
                out.update(on_side(up))
        return out

    def getLabel(self) -> str:
        return self.compileListData("labels", ["name"])

    def getCatno(self) -> str:
        return self.compileListData("labels", ["catno"])

    def getCountry(self) -> str:
        return _text(self.data.get("country"))

    def getFormats(self) -> list[str]:
        """The release's format names in Discogs order, e.g. ["Box Set", "Vinyl", "CD"].

        One entry per format line, not per item: 3×12" plus a CD is
        ["Vinyl", "CD"], with the counts in each format's `qty`.
        """
        names = (_text(f.get("name")) for f in self.data.get("formats") or [])
        return [n for n in names if n]

    def getGenre(self) -> str:
        genres = [_text(g) for g in self.data.get("styles") or []]
        if not any(genres):
            genres = [_text(g) for g in self.data.get("genres") or []]
        return ", ".join(g for g in genres if g)

    def getTitle(self, synthesise: bool = True) -> str:
        rt = _text(self.data.get("title"))
        if not rt.startswith("Untitled") or not synthesise:
            return rt

        return self.getCatno()

    def getArtwork(self) -> bytes | None:
        if self.imgdata:
            return self.imgdata

        images = self.data.get("images", [])
        if not images:
            return None

        uri = images[0]["uri"]
        # MD5 gives a stable, filesystem-safe filename for the cache.
        # hash() is explicitly avoided: it is randomised by PYTHONHASHSEED,
        # so using it would orphan cached files on every interpreter restart.
        hashuri = hashlib.md5(uri.encode()).hexdigest()

        with discogs_lock:
            if os.path.exists(util.userfile(hashuri)):
                with open(util.userfile(hashuri), "rb") as fo:
                    imgdata = fo.read()
            else:
                count = 0
                imgdata = b""
                while (True):
                    time.sleep(1.05)
                    count = count + 1
                    try:
                        req = urllib.request.Request(uri, data=None, headers=url_headers)
                        imgdata = urllib.request.urlopen(req).read()
                        break
                    except urllib.error.HTTPError as err:
                        print("Error fetching cover art")
                        print("URL: ", uri)
                        print(err.code, err.reason)
                        print(err.headers)
                        raise
                    except urllib.error.URLError:
                        if (count < 5):
                            continue
                        else:
                            raise

                with open(util.userfile(hashuri), "wb") as fo:
                    fo.write(imgdata)

        self.imgdata = _normalize_artwork(imgdata)
        return self.imgdata

    def __repr__(self) -> str:
        return "<DiscogsRelease %d>" % self.rid

    def __str__(self) -> str:
        return "%s - %s (%s: %s)" % (self.getArtist(), self.getTitle(),
                self.getLabel(), self.getCatno())


class DiscogsTrack:
    tdata: DiscogsTrackData
    release: DiscogsRelease
    index: int

    def __getitem__(self, key: str) -> str | list[DiscogsArtistRef]:
        return self.tdata[key]  # type: ignore[literal-required]

    # Index is from 0
    def __init__(self, release: DiscogsRelease | int, index: int) -> None:
        if isinstance(release, int):
            self.release = DiscogsRelease(release)
        else:
            self.release = release
        self.index = index
        try:
            self.tdata = self.release.data["tracklist"][index]
        except IndexError:
            raise ClientException("Release %s has no track %d" % (release, index))

    def __repr__(self) -> str:
        return "<DiscogsTrack %d:%d>" % (self.release.rid, self.index)

    def __str__(self) -> str:
        return "%s - %s [%s] - %s: %s" % (self.getArtist(), self.release.getTitle(),
                self.release.getLabel(),
                self.tdata["position"], self.getTitle())

    def getTrackNumber(self) -> int:
        return self.index + 1

    def getArtist(self) -> str:
        artists = self.tdata.get("artists")
        if not artists:
            return self.release.getArtist()

        return " / ".join(n for n in (_text(a.get("name")) for a in artists) if n)

    def getTitle(self, synthesise: bool = True) -> str:
        t = _text(self.tdata.get("title"))
        if not t.startswith("Untitled") or not synthesise:
            return t

        return "%s %s" % (self.release.getTitle(), self.getPosition())

    def getPosition(self) -> str:
        return _text(self.tdata.get("position"))

    def getArtistIds(self) -> set[int]:
        """Discogs ids of this track's performing artists.

        Tracks without their own `artists` inherit the release's, the same
        fallback getArtist() uses.
        """
        artists = self.tdata.get("artists")
        if not artists:
            return self.release.getArtistIds()
        return {a["id"] for a in artists if a.get("id")}

    def getTrackCredits(self) -> list[DiscogsCredit]:
        """Credits recorded on this track itself, one entry per role.

        These are the ones Discogs shows under the track in its tracklist —
        "Remix – Norman Nodge".  Release credits, even ones scoped to this
        track, are not included; see getCredits() for those.
        """
        found: list[DiscogsCredit] = []
        for entry in self.tdata.get("extraartists") or []:
            found.extend(_credits_from(entry))
        return found

    def getCredits(self) -> list[DiscogsCredit]:
        """Every credit that applies to this track, one entry per role.

        Discogs records credits in two places: on the track itself
        (getTrackCredits), and on the release with a `tracks` field saying
        which tracks they cover.  Both are merged here.  Release credits with no
        `tracks` field cover every track, so they are included too.

        Labels print only the track's own credits; this fuller view is kept
        for callers that want what Discogs lists on the release as well.

        Order follows Discogs (track credits first); exact duplicates, which
        occur when both places record the same credit, are dropped.
        """
        found: list[DiscogsCredit] = list(self.getTrackCredits())
        for entry in self.release.data.get("extraartists") or []:
            scope = self.release.resolveTrackScope(entry.get("tracks", ""))
            if scope is None or self.index in scope:
                found.extend(_credits_from(entry))

        seen: set[tuple[str, int | str]] = set()
        unique: list[DiscogsCredit] = []
        for c in found:
            key = (c["role"], c["artist_id"] or c["name"])
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    def getDuration(self) -> str:
        return _text(self.tdata.get("duration"))

    def getRelease(self) -> DiscogsRelease:
        return self.release


if __name__ == "__main__":
    pp = pprint.PrettyPrinter()
    c = get_client_instance()

    t = DiscogsTrack(int(sys.argv[1]), 0)
    pp.pprint(t.release.data)
    print((repr(t)))
    print(t)
