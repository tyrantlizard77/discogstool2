import mutagen
import mutagen.id3
import sys
import pprint
import urllib.request, urllib.parse, urllib.error
import os.path
import shutil
import filecmp
import re
import imghdr
from typing import cast
# MutagenFileType is imported from mutagen's private _file module because
# mutagen does not export FileType from its public namespace.  This is an
# acknowledged wart: mutagen.File() exists at runtime but isn't in __all__,
# so Pyright can't see it without reaching into the private module.  If a
# future mutagen release breaks this, the fix is to write a local stub.
from mutagen._file import FileType as MutagenFileType
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Cover
import client_interface

whitelist = frozenset([i for i in "1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ[]()-_+.' "])

def sanitize(fn: str) -> str:
    return "".join([i if (i in whitelist) else "_" for i in fn])

tag_map: dict[str, dict[str, str]] = {
    "ID3" : {
        "album" : "TALB",
        "artist" : "TPE1",
        "bpm" : "TBPM",
        "title" : "TIT2",
        "year" : "TDRC",
        "comment" : "COMM::eng",
        "genre" : "TCON",
        "image" : "APIC",
        "track" : "TRCK",
        "label" : "TPUB",
        "compilation" : "TCMP",
        },
    "MP4Tags" : {
        "album" : "\xa9alb",
        "artist" : "\xa9ART",
        "bpm" : "tmpo",
        "title" : "\xa9nam",
        "year" : "\xa9day",
        "comment" : "\xa9cmt",
        "genre" : "\xa9gen",
        "image" : "covr",
        "track" : "trkn",
        "label" : "\xa9lab",
        "compilation" : "cpil",
    }
}

rev_tag_map: dict[str, str] = {}
for k,v in tag_map.items():
    for k2,v2 in tag_map[k].items():
        rev_tag_map[v2] = k2

uni_flag = "\xa9"

old_comment_regex = re.compile(r"([0-9]+) VERIFIED");
comment_regex = re.compile(r".* Discogs: ([0-9]+)");

class TagsException(Exception):
    pass

def track_from_comment(comment: str, index: int) -> client_interface.DiscogsTrack:
    m = comment_regex.match(comment)
    if not m:
        m = old_comment_regex.match(comment)

    if not m:
        raise TagsException("comment '%s' doesn't specify a release" % comment)

    return client_interface.DiscogsTrack(int(m.groups()[0]), index - 1)


class AudioFile(object):
    def __init__(self, filename: str, track: client_interface.DiscogsTrack | None = None) -> None:
        self.filename = filename
        self.obj: MutagenFileType | None = mutagen.File(filename)  # type: ignore[reportAttributeAccessIssue]
        if self.obj is None:
            raise TagsException("mutagen couldn't open " + filename)

        if not self.obj.tags:
            self.obj.add_tags()

        assert self.obj.tags is not None

        if issubclass(self.obj.tags.__class__, ID3):
            self.tagstype = "ID3"
        else:
            self.tagstype = self.obj.tags.__class__.__name__

        if not track:
            if not self["comment"] or not self["track"]:
                raise TagsException("file has no comment or track number information")

            track_tuple = cast(tuple[int, int], self["track"])
            track = track_from_comment(cast(str, self["comment"]), track_tuple[0])

            if track_tuple[1] != track.getRelease().getTotalTracks():
                raise TagsException("total tracks mismatch")

        self.update(track)

    def getTrack(self) -> client_interface.DiscogsTrack:
        return self.track

    def getFilename(self) -> str:
        return self.filename

    def __getitem__(self, key: str) -> object:
        if key == "filename":
            return self.filename
        assert self.obj is not None
        assert self.obj.tags is not None
        try:
            i: object = self.obj.tags[tag_map[self.tagstype][key]]
        except KeyError:
            return None
        while isinstance(i, list):
            i = i[0]
        if key == "track":
            # MP4 stores track as tuple (track, total), ID3 as string "track/total"
            if isinstance(i, tuple):
                i = (int(i[0]), int(i[1]) if len(i) > 1 and i[1] else 0)
            else:
                parts = str(i).split("/")
                if len(parts) == 1:
                    parts.append("0")
                i = tuple([int(x) for x in parts])
        elif key == "bpm":
            i = int(str(i))
        elif key == "image":
            i = "<binary>"
        else:
            i = str(i)
        return i

    def update(self, track: client_interface.DiscogsTrack) -> None:
        release = track.release

        self["artist"] = track.getArtist()
        self["album"] = release.getTitle()
        self["title"] = track.getTitle()
        self["year"] = release.getYear()
        self["comment"] = "%s [%s] Discogs: %d" % (release.getLabel(),
                release.getCatno(), release.getId())
        self["label"] = release.getLabel()
        self["track"] = (track.getTrackNumber(), release.getTotalTracks())
        # Don't overwrite Genre set already
        if not self["genre"]:
            self["genre"] = release.getGenre()
        if "image" not in list(self.keys()):
            i = release.getArtwork()
            if i:
                self["image"] = i
        if release.isCompilation():
            self["compilation"] = 1
        self.track = track

    def commit(self) -> None:
        self.save()

    def __setitem__(self, key: str, value: object) -> None:
        assert self.obj is not None
        assert self.obj.tags is not None
        mkey = tag_map[self.tagstype][key]
        if self.tagstype == "ID3":
            clazz = getattr(mutagen.id3, mkey[:4])
            if mkey == "COMM::eng":
                value = clazz(encoding=3, desc="", lang='eng', text=value)
            elif mkey == "APIC":
                self.obj.tags.delall("APIC")
                raw_fmt = imghdr.what(None, h=cast(bytes, value))
                mimetype = "image/" + (raw_fmt or "jpeg")
                value = clazz(type=0, encoding=0, mime=mimetype, data=value)
            else:
                if mkey == "TRCK":
                    track_val = cast(tuple[int, int], value)
                    if track_val[1]:
                        value = "%d/%d" % track_val
                    else:
                        value = str(track_val[0])
                elif mkey == "TCMP":
                    value = str(value)
                # mutagen id3 can't seem to handle unicode values
                value = clazz(encoding=3, text=value)
        elif self.tagstype == "MP4Tags":
            if mkey == "trkn":
                value = [value]
            elif mkey == "covr":
                raw_bytes = cast(bytes, value)
                # Detect image format for MP4Cover
                if raw_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                    fmt = MP4Cover.FORMAT_PNG
                else:
                    fmt = MP4Cover.FORMAT_JPEG
                value = [MP4Cover(raw_bytes, imageformat=fmt)]

        self.obj.tags[mkey] = value

    def save(self) -> None:
        assert self.obj is not None
        self.obj.save()

    def keys(self) -> list[str]:
        assert self.obj is not None
        assert self.obj.tags is not None
        ok = list(self.obj.tags.keys())
        ret: list[str] = []
        for k,v in tag_map[self.tagstype].items():
            if v in ok:
                ret.append(k)
        return ret

    def __str__(self) -> str:
        ret: dict[str, object] = {}
        for k in list(self.keys()):
            ret[k] = self[k]
        return repr(ret)

    def rename_file(self, destdir: str, verbose: bool, dryrun: bool, move: bool, withgenre: bool) -> str | None:
        af = self
        ext = self.filename.rsplit(".", 1)[1]
        if withgenre:
            if af["genre"] == "null":
                print(("Skipping genre unassigned", self.filename))
                return None
            newdir = os.path.join(destdir, sanitize(str(af["genre"])))
        else:
            newdir = destdir

        try:
            bpm = int(str(af["bpm"]))
        except ValueError:
            bpm = 0
        if withgenre:
            newfn = sanitize("[%03d] %s - %s %d (%s).%s" %
                    (bpm, af["artist"], af["title"], cast(tuple[int, int], af["track"])[0], af["year"], ext))
        else:
            catno = self.track.release.getCatno()
            newfn = sanitize("%s - %d - %s - %s [%s].%s" %
                    (af["album"], cast(tuple[int, int], af["track"])[0], af["artist"], af["title"], catno, ext))

        newpath = os.path.abspath(os.path.join(newdir, newfn))
        if not os.path.exists(newpath) or not filecmp.cmp(self.filename, newpath):
            if verbose:
                print("MOVE" if move else "COPY", self.filename, "\n\t-->", newpath)
            if not dryrun:
                if not os.path.exists(newdir):
                    os.makedirs(newdir)
                if move:
                    shutil.move(self.filename, newpath)
                    self.filename = newpath
                else:
                    shutil.copyfile(self.filename, newpath)
        else:
            if verbose:
                print("Skipping unchanged file", newpath)
        return newpath

# debugging only
def main() -> None:
    for filename in sys.argv[1:]:
        af = AudioFile(filename)
        print(filename)
        for k in list(af.keys()):
            if k not in ["image"]:
                print("%s: '%s'" % (k, af[k]))

if __name__ == "__main__":
    main()
