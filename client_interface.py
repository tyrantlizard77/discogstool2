#!/usr/bin/env python3

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
import ctypes
import io
from PIL import Image

discogs_auth = util.userfile("discogs_auth")
useragent = "discogstool/2.0"
consumer_key = "mWCofNBrngwtGCSBOTDe"
consumer_secret = "nBgWYPSMtAonLobnAuZiowpJyUzhbcgW"
cached_instance = None

url_headers = {
    'User-Agent' : useragent
}

discogs_lock = multiprocessing.Lock()

class ClientException(Exception):
    pass

def get_user_auth_tokens():
    if os.path.exists(discogs_auth):
        try:
            with open(discogs_auth, "r") as fp:
                token, secret = fp.read().split("|")
        except Exception:
            os.unlink(discogs_auth)
            return None, None
        return str(token), str(secret)
    else:
        return None, None

def set_user_auth_tokens(token, secret):
    with open(discogs_auth, "w") as fp:
        fp.write("%s|%s" % (token, secret))

def get_client_instance():
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

def scrub_data(data):
    if isinstance(data, dict):
        for key, item in list(data.items()):
            data[key] = scrub_data(item)
        return data
    if isinstance(data, list):
        return [scrub_data(i) for i in data]
    if isinstance(data, str) or isinstance(data, str):
        return data.strip()
    return data

threadlocal = threading.local()


def _normalize_artwork(imgdata):
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

    def __getitem__(self, key):
        return self.data[key]

    def getData(self, rid):
        db = getattr(threadlocal, "db", None)
        if db is None:
            db = database.DiscogsDatabase()
            threadlocal.db = db

        key = "release-%d" % rid
        data = db.get(key)
        if data:
            if "tracklist" in data:
                return data
            else:
                db.delete(key)

        with discogs_lock:
            client = get_client_instance()

            # Even though rate limiting properly, still see transient
            # "Connection Reset By Peer" and "Bad Status Line" errors
            success = False
            for i in range(3):
                try:
                    time.sleep(1.1 + (5 * i))
                    release = client.release(rid)
                    release.refresh()
                except Exception as e:
                    continue
                success = True
                break

            if not success:
                raise ClientException("release %d couldn't be fetched" % rid)

        data = scrub_data(release.data)
        db.put(key, data)

        return data

    def __init__(self, rid):
        self.rid = rid
        self.data = scrub_data(self.getData(rid))

        # Filter out non-track items in the tracklist like headings
        self.data["tracklist"] = [i for i in self.data["tracklist"]
                if i["type_"] == "track" and i["title"] != ""]
        self.totaltracks = len(self.data["tracklist"])
        self.imgdata = None

    def isCompilation(self):
        art = self.getTrack(0).getArtist()

        for i in range(1, self.getTotalTracks()):
            if self.getTrack(i).getArtist() != art:
                return True

        return False

    def getId(self):
        return self.rid

    def getTotalTracks(self):
        return self.totaltracks

    def getTrack(self, index):
        return DiscogsTrack(self, index)

    def getYear(self):
        return str(self.data["year"])

    def compileListData(self, listname, keys):
        for key in keys:
            s = self.data[listname][0][key]
            if s:
                break

        for i in self.data[listname][1:]:
            for key in keys:
                x = i[key]
                if x:
                    break
            s = "%s / %s" % (s, x)
        return s.strip()

    def getArtist(self):
        return self.compileListData("artists", ["anv", "name"])

    def getLabel(self):
        return self.compileListData("labels", ["name"])

    def getCatno(self):
        return self.compileListData("labels", ["catno"])

    def getCountry(self):
        return self.data.get("country", "")

    def getGenre(self):
        genres = self.data.get("styles", [])
        if not genres:
            genres = self.data.get("genres", [])
        return ", ".join(genres)

    def getTitle(self):
        rt = self.data["title"]
        if not rt.startswith("Untitled"):
            return rt

        return self.getCatno()

    def getArtwork(self):
        if self.imgdata:
            return self.imgdata

        if "images" not in self.data:
            return None

        uri = self.data["images"][0]["uri"]
        hashuri = hex(ctypes.c_uint64(hash(uri)).value)

        with discogs_lock:
            if os.path.exists(util.userfile(hashuri)):
                with open(util.userfile(hashuri), "rb") as fo:
                    imgdata = fo.read()
            else:
                count = 0
                while (True):
                    time.sleep(1.05)
                    count = count + 1
                    try:
                        req = urllib.request.Request(uri, data=None, headers=url_headers)
                        imgdata = urllib.request.urlopen(req).read()
                        break
                    except urllib.error.URLError as err:
                        if (count < 5):
                            continue
                        else:
                            raise
                    except urllib.error.HTTPError as err:
                        print("Error fetching cover art")
                        print("URL: ", uri)
                        print(err.code, err.reason)
                        print(err.headers)
                        raise

                with open(util.userfile(hashuri), "wb") as fo:
                    fo.write(imgdata)

        self.imgdata = _normalize_artwork(imgdata)
        return self.imgdata

    def __repr__(self):
        return "<DiscogsRelease %d>" % self.rid

    def __str__(self):
        return "%s - %s (%s: %s)" % (self.getArtist(), self.getTitle(),
                self.getLabel(), self.getCatno())

class DiscogsTrack:
    def __getitem__(self, key):
        return self.tdata[key]

    # Index is from 0
    def __init__(self, release, index):
        if isinstance(release, int):
            self.release = DiscogsRelease(release)
        else:
            self.release = release
        self.index = index
        try:
            self.tdata = self.release["tracklist"][index]
        except IndexError as ie:
            raise ClientException("Release %d has no track %d" % (release, index))

    def __repr__(self):
        return "<DiscogsTrack %d:%d>" % (self.release.rid, self.index)

    def __str__(self):
        relstr = str(self.release)
        return "%s - %s [%s] - %s: %s" % (self.getArtist(), self.release.getTitle(),
                self.release.getLabel(),
                self.tdata["position"], self.getTitle())

    def getTrackNumber(self):
        return self.index + 1

    def getArtist(self):
        if "artists" not in self.tdata:
            return self.release.getArtist()

        artiststr = self.tdata["artists"][0]["name"]
        for artist in self.tdata["artists"][1:]:
            artiststr = "%s / %s" % (artiststr, artist["name"])
        return artiststr.strip()

    def getTitle(self):
        t = self.tdata["title"]
        if not t.startswith("Untitled"):
            return t

        return "%s %s" % (self.release.getTitle(), self.tdata["position"])

    def getRelease(self):
        return self.release


if __name__ == "__main__":
    pp = pprint.PrettyPrinter()
    c = get_client_instance()

    t = DiscogsTrack(int(sys.argv[1]), 0)
    pp.pprint(t.release.data)
    print((repr(t)))
    print(t)



