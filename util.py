from __future__ import annotations

import os
import csv

datapath = os.path.expanduser(os.path.join("~", ".discogstool"))

if not os.path.exists(datapath):
    os.mkdir(datapath)


def userfile(fname: str) -> str:
    return os.path.join(datapath, fname)


def file_extension(path: str) -> str:
    _, ext = os.path.splitext(path)
    return ext[1:].lower()


def get_audio_files(basedir: str) -> list[str]:
    filelist: list[str] = []

    for root, dirs, files in os.walk(basedir):
        for fname in files:
            if fname.startswith("."):
                continue
            if file_extension(fname) in ["mp3", "m4a", "aac", "mp4", "aiff", "aif"]:
                filename = os.path.abspath(os.path.join(root, fname))
                try:
                    filelist.append(filename)
                except Exception as e:
                    print(("Failed to process", filename))
                    raise e

    return filelist


class CollectionInfo:
    releaseid: int
    collection: str
    date: str
    mcond: str
    scond: str
    notes: str

    def __init__(
        self,
        releaseid: int,
        collection: str,
        date: str,
        mcond: str,
        scond: str,
        notes: str,
    ) -> None:
        self.releaseid = releaseid
        self.collection = collection
        self.date = date
        self.mcond = mcond
        self.scond = scond
        self.notes = notes

# CSV format:
# catno, artist, title, label, format, rating, released, id, collection, date added
# media condition, sleeve condition, notes
def parse_collection_xml(path: str) -> list[CollectionInfo]:
    collection: list[CollectionInfo] = []

    with open(path, "r") as csvfile:
        reader = csv.reader(csvfile)
        next(reader)  # skip header row
        for line in reader:
            releaseid = int(line[7])

            # Items prior from 8 can be derived from the release object
            line = line[8:]

            coll, date, mcond, scond, notes = line
            ci = CollectionInfo(releaseid, coll, date, mcond, scond, notes)

            collection.append(ci)

    return collection

