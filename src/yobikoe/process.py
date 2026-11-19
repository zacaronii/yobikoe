"""Even out recordings: trim leading/trailing silence and bring every clip to the same loudness.

Recordings come from many sources (NHK studio takes, Forvo users' microphones...) and their levels differ a lot.
Each clip is measured once with ffmpeg; only the result is kept (how many frames of silence to drop at each end,
and a volume change), in a small SQLite file. Every play then applies it to the original mp3 losslessly
(`mp3edit`: whole frames dropped, per-frame gain changed), so there's no second copy of the pack and no re-encoding.
A clip `mp3edit` can't handle is re-encoded with ffmpeg instead; if ffmpeg is missing or fails, the original is used.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import mp3edit

log = logging.getLogger(__name__)

TARGET_MEAN_DB = -20.0      # average level every clip is brought to
PEAK_LIMIT_DB = -1.0        # ...without pushing peaks above this
SILENCE_DB = "-50dB"        # quieter than this at the start/end counts as silence
_TRIM = (f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_silence=0.05,areverse,"
         f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_silence=0.1,areverse")
# One pass with the same trimming: astats counts the samples before, after the leading trim and after both trims
# (the differences are the silence to drop) and reads the true peak (above 0 dB on clipped recordings, where
# volumedetect stops at 0); volumedetect gives the mean.
_COUNT = "astats@{}=measure_perchannel=none:measure_overall=Number_of_samples+Peak_level"
_ANALYSE = (f"{_COUNT.format('all')},silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_silence=0.05,"
            f"{_COUNT.format('lead')},areverse,silenceremove=start_periods=1:start_threshold={SILENCE_DB}:"
            f"start_silence=0.1,areverse,{_COUNT.format('kept')},volumedetect")


@dataclass(frozen=True)
class Levels:
    head: int = 0           # audio frames to drop at the start
    tail: int = 0           # ...and at the end
    steps: int = 0          # global_gain steps (~1.5 dB each)

    @property
    def identity(self) -> bool:
        return not (self.head or self.tail or self.steps)


class LevelStore:
    """member -> Levels, in its own SQLite file (the index is opened read-only). Cleared when the pack changes."""

    def __init__(self, path: Path, stamp: str = ""):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self._db.execute("CREATE TABLE IF NOT EXISTS levels (member TEXT PRIMARY KEY, head INTEGER NOT NULL,"
                         " tail INTEGER NOT NULL, steps INTEGER NOT NULL) WITHOUT ROWID")
        row = self._db.execute("SELECT value FROM meta WHERE key='pack'").fetchone()
        if stamp and (row is None or row[0] != stamp):
            if row is not None:
                log.info("audio pack changed: measuring recordings again")
            self._db.execute("DELETE FROM levels")
            self._db.execute("INSERT OR REPLACE INTO meta VALUES ('pack', ?)", (stamp,))

    def get(self, member: str) -> Levels | None:
        with self._lock:
            row = self._db.execute("SELECT head, tail, steps FROM levels WHERE member=?", (member,)).fetchone()
        return Levels(*row) if row else None

    def put(self, member: str, levels: Levels) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO levels VALUES (?,?,?,?)",
                             (member, levels.head, levels.tail, levels.steps))

    def missing(self, members: list[str]) -> list[str]:
        if not members:
            return []
        with self._lock:
            have = {r[0] for r in self._db.execute(
                f"SELECT member FROM levels WHERE member IN ({','.join('?' * len(members))})", members)}
        return [m for m in members if m not in have]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT count(*) FROM levels").fetchone()[0]


class Processor:
    def __init__(self, levels: LevelStore, ffmpeg: str | None = None, timeout: float = 15.0, nice: bool = False):
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.levels, self.timeout = levels, timeout
        self.prefix = ["nice", "-n", "19"] if nice and shutil.which("nice") else []
        if not self.ffmpeg:
            log.warning("ffmpeg not found: recordings are served unprocessed")

    @property
    def enabled(self) -> bool:
        return bool(self.ffmpeg)

    def get(self, key: str, data: bytes) -> bytes:
        """`data` trimmed and levelled (measured once, stored under `key`), or `data` itself when that isn't possible."""
        if not self.ffmpeg:
            return data
        levels = self.measure(key, data)
        if levels is None or levels.identity:
            return data
        try:
            return mp3edit.edit(data, levels.head, levels.tail, levels.steps)
        except mp3edit.Mp3Error as e:
            log.info("%s: can't edit in place (%s), re-encoding", key, e)
        try:
            return self.process(data)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.warning("processing %s failed, serving the original: %s", key, e)
            return data

    def measure(self, key: str, data: bytes) -> Levels | None:
        """Stored levels for `key`, measuring (and storing) them first if needed. None if ffmpeg failed."""
        levels = self.levels.get(key)
        if levels is not None:
            return levels
        try:
            levels = self.analyse(data)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("measuring %s failed, serving the original: %s", key, e)
            return None
        self.levels.put(key, levels)
        return levels

    def analyse(self, data: bytes) -> Levels:
        """Measure silence and loudness; silent or unreadable clips get Levels() (left as they are)."""
        m = parse_analysis(self._run(["-af", _ANALYSE, "-f", "null", "-"], data, want_stderr=True))
        if m is None:
            return Levels()
        steps = gain_steps(m.mean, m.peak)
        try:
            t = mp3edit.timing(data)
        except mp3edit.Mp3Error:
            return Levels(0, 0, steps)       # get() re-encodes these (process() measures again itself)
        rate = t.sample_rate
        head, tail = mp3edit.frames_for_trim(t, m.lead / rate, (m.total - m.trail) / rate if m.trail else None)
        return Levels(head, tail, steps)

    def process(self, data: bytes) -> bytes:
        """Fallback for clips mp3edit can't edit: trim + level by decoding and re-encoding."""
        stats = self._run(["-af", _TRIM + ",volumedetect", "-f", "null", "-"], data, want_stderr=True)
        mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", stats)
        peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", stats)
        if not mean or not peak:
            raise ValueError("ffmpeg reported no volume (empty or silent clip)")
        gain = min(TARGET_MEAN_DB - float(mean.group(1)), PEAK_LIMIT_DB - float(peak.group(1)))
        return self._run(["-af", f"{_TRIM},volume={gain:.2f}dB", "-codec:a", "libmp3lame", "-q:a", "4",
                          "-f", "mp3", "-"], data)

    def _run(self, args: list[str], data: bytes, want_stderr: bool = False):
        r = subprocess.run([*self.prefix, self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info" if want_stderr else "error",
                            "-i", "pipe:0", *args], input=data, capture_output=True, timeout=self.timeout, check=True)
        return r.stderr.decode("utf-8", "replace") if want_stderr else r.stdout


def gain_steps(mean_db: float, peak_db: float) -> int:
    """Whole gain steps toward TARGET_MEAN_DB, never pushing the peak above PEAK_LIMIT_DB."""
    steps = round((TARGET_MEAN_DB - mean_db) / mp3edit.STEP_DB)
    return min(steps, math.floor((PEAK_LIMIT_DB - peak_db) / mp3edit.STEP_DB))


@dataclass(frozen=True)
class Analysis:
    total: int              # decoded samples
    lead: int               # samples silenceremove drops at the start
    trail: int              # ...and at the end
    mean: float             # dB, after trimming
    peak: float             # dB, true peak (can be above 0)


def parse_analysis(stats: str) -> Analysis | None:
    """None for a silent or unreadable clip."""
    counts = dict(re.findall(r"\[astats@(\w+) @ [^\]]*\] Number of samples: (\d+)", stats))
    peak = re.search(r"\[astats@kept @ [^\]]*\] Peak level dB: (-?[\d.]+|-?inf)", stats)
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", stats)
    if not mean or not peak or peak.group(1).endswith("inf") or not {"all", "lead", "kept"} <= counts.keys():
        return None
    total, lead_left, kept = int(counts["all"]), int(counts["lead"]), int(counts["kept"])
    if kept == 0:
        return None
    return Analysis(total, max(0, total - lead_left), max(0, lead_left - kept), float(mean.group(1)),
                    float(peak.group(1)))


class Warmer:
    """Measures every recording in the background (lowest CPU priority), most-played sources first, so first
    plays don't wait for ffmpeg. Restarts resume: already-measured clips are skipped."""

    def __init__(self, store, processor: Processor, sources: list[str], workers: int = 2):
        self.store, self.processor, self.sources, self.workers = store, processor, sources, workers
        self.done = self.skipped = self.failed = 0
        self.total = 0
        self.finished = False

    def start(self) -> None:
        threading.Thread(target=self._run, name="warmer", daemon=True).start()

    def _run(self) -> None:
        order = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(self.sources))
        rows = self.store.db.execute(f"SELECT DISTINCT source, file FROM entries ORDER BY CASE source {order} ELSE 99 END"
                                     ).fetchall()
        self.total = len(rows)
        log.info("warmer: %d recordings to check", self.total)

        def one(member):
            data = self.store.read(member)
            if data is None:
                self.failed += 1
                return
            self.processor.measure(member, data)
            self.done += 1

        with ThreadPoolExecutor(self.workers, thread_name_prefix="warm") as pool:
            for start in range(0, len(rows), 256):          # feed the pool in batches: bounded memory
                batch = [f"{s}_files/{f}" for s, f in rows[start:start + 256]]
                todo = self.processor.levels.missing(batch)
                self.skipped += len(batch) - len(todo)
                list(pool.map(one, todo))
        self.finished = True
        log.info("warmer: finished (%d measured, %d already measured, %d missing)", self.done, self.skipped, self.failed)

    def status(self) -> dict:
        return {"total": self.total, "processed": self.done, "already_cached": self.skipped, "missing": self.failed,
                "finished": self.finished}
