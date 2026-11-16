"""The audio pack: a zip of mp3 folders (`<source>_files/...`) plus `entry_and_pitch_db.sql`.

`build()` turns the pack into one SQLite file: the dump's `entries` and `pitch_accents` tables as they are, and a
`files` table with where each mp3's bytes sit inside the zip. `AudioStore` then serves a file with two `pread`s and
a small inflate, without keeping the zip's 485k-entry directory in memory.
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
import struct
import threading
import zipfile
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SQL_DUMP = "entry_and_pitch_db.sql"
_LOCAL_HEADER = struct.Struct("<4s5H3I2H")       # zip local file header, 30 bytes
_LOCAL_SIGNATURE = b"PK\x03\x04"


def _pack_stamp(zip_path: Path) -> str:
    st = zip_path.stat()
    return f"{SCHEMA_VERSION}:{st.st_size}:{int(st.st_mtime)}"


def is_current(db_path: Path, zip_path: Path) -> bool:
    """True when db_path was built from this exact pack (same size and mtime) by this schema version."""
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as c:
            row = c.execute("SELECT value FROM meta WHERE key='pack'").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == _pack_stamp(zip_path)


def build(zip_path: Path, db_path: Path) -> None:
    """Import the pack's SQL dump and the zip's file locations into a fresh SQLite file, then swap it in."""
    tmp = db_path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    with zipfile.ZipFile(zip_path) as z:
        log.info("importing %s", SQL_DUMP)
        with z.open(SQL_DUMP) as f:
            script = io.TextIOWrapper(f, encoding="utf-8").read()
        c.executescript("BEGIN;" + script + ";COMMIT;")
        log.info("indexing %d zip members", len(z.infolist()))
        c.execute("CREATE TABLE files (name TEXT PRIMARY KEY, offset INTEGER NOT NULL, csize INTEGER NOT NULL,"
                  " size INTEGER NOT NULL, method INTEGER NOT NULL) WITHOUT ROWID")
        c.executemany("INSERT INTO files VALUES (?,?,?,?,?)",
                      ((i.filename, i.header_offset, i.compress_size, i.file_size, i.compress_type)
                       for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".mp3")))
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT INTO meta VALUES ('pack', ?)", (_pack_stamp(zip_path),))
    c.commit()
    c.execute("ANALYZE")
    c.close()
    tmp.replace(db_path)
    log.info("index ready: %s", db_path)


class AudioStore:
    """Read-only access to the built index and the zip."""

    def __init__(self, zip_path: Path, db_path: Path):
        self.zip_path, self.db_path = zip_path, db_path
        self._fd = os.open(zip_path, os.O_RDONLY)
        self._local = threading.local()

    @property
    def db(self) -> sqlite3.Connection:
        """One read-only connection per thread (FastAPI runs sync endpoints in a thread pool)."""
        c = getattr(self._local, "db", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.db = c
        return c

    def read(self, name: str) -> bytes | None:
        """The mp3 at zip member `name`, or None if the pack has no such file."""
        row = self.db.execute("SELECT offset, csize, size, method FROM files WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        header = os.pread(self._fd, _LOCAL_HEADER.size, row["offset"])
        fields = _LOCAL_HEADER.unpack(header)
        if fields[0] != _LOCAL_SIGNATURE:
            raise ValueError(f"bad zip local header for {name}")
        name_len, extra_len = fields[9], fields[10]
        data = os.pread(self._fd, row["csize"], row["offset"] + _LOCAL_HEADER.size + name_len + extra_len)
        if row["method"] == zipfile.ZIP_STORED:
            return data
        if row["method"] == zipfile.ZIP_DEFLATED:
            return zlib.decompress(data, -15)
        raise ValueError(f"unsupported compression {row['method']} for {name}")

    def close(self) -> None:
        os.close(self._fd)
