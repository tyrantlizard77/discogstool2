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
    duration: str


class _DiscogsReleaseDataRequired(TypedDict):
    tracklist: list[DiscogsTrackData]
    title: str
    artists: list[DiscogsArtistRef]
    labels: list[DiscogsLabelRef]


class DiscogsReleaseData(_DiscogsReleaseDataRequired, total=False):
    year: int
    images: list[DiscogsImageRef]
    country: str
    styles: list[str]
    genres: list[str]


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

    def getYear(self) -> str:
        return str(self.data.get("year", ""))

    def compileListData(self, listname: str, keys: list[str]) -> str:
        items = cast(list[dict[str, str]], self.data[listname])  # type: ignore[literal-required]
        s: str = ""
        for key in keys:
            s = items[0].get(key, "")
            if s:
                break

        for i in items[1:]:
            x: str = ""
            for key in keys:
                x = i.get(key, "")
                if x:
                    break
            s = "%s / %s" % (s, x)
        return s.strip()

    def getArtist(self) -> str:
        return self.compileListData("artists", ["anv", "name"])

    def getLabel(self) -> str:
        return self.compileListData("labels", ["name"])

    def getCatno(self) -> str:
        return self.compileListData("labels", ["catno"])

    def getCountry(self) -> str:
        return self.data.get("country", "")

    def getGenre(self) -> str:
        genres: list[str] = self.data.get("styles", [])
        if not genres:
            genres = self.data.get("genres", [])
        return ", ".join(genres)

    def getTitle(self, synthesise: bool = True) -> str:
        rt = self.data["title"]
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

        artiststr = artists[0].get("name", "")
        for artist in artists[1:]:
            artiststr = "%s / %s" % (artiststr, artist.get("name", ""))
        return artiststr.strip()

    def getTitle(self, synthesise: bool = True) -> str:
        t = self.tdata["title"]
        if not t.startswith("Untitled") or not synthesise:
            return t

        return "%s %s" % (self.release.getTitle(), self.tdata["position"])

    def getPosition(self) -> str:
        return self.tdata["position"]

    def getDuration(self) -> str:
        return self.tdata.get("duration", "").strip()

    def getRelease(self) -> DiscogsRelease:
        return self.release


if __name__ == "__main__":
    pp = pprint.PrettyPrinter()
    c = get_client_instance()

    t = DiscogsTrack(int(sys.argv[1]), 0)
    pp.pprint(t.release.data)
    print((repr(t)))
    print(t)
