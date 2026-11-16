"""Even out recordings: trim leading/trailing silence and bring every clip to the same loudness (ffmpeg).

Recordings come from many sources (NHK studio takes, Forvo users' microphones...) and their levels differ a lot.
Each clip is processed once, on first play, and cached; the audio pack itself is never changed. If ffmpeg is missing
or fails, the original clip is used.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_MEAN_DB = -20.0      # average level every clip is brought to
PEAK_LIMIT_DB = -1.0        # ...without pushing peaks above this
SILENCE_DB = "-50dB"        # quieter than this at the start/end counts as silence
_TRIM = (f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_silence=0.05,areverse,"
         f"silenceremove=start_periods=1:start_threshold={SILENCE_DB}:start_silence=0.1,areverse")


class Processor:
    def __init__(self, cache_dir: Path, ffmpeg: str | None = None, timeout: float = 15.0, nice: bool = False):
        self.ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
        self.cache_dir, self.timeout = cache_dir, timeout
        self.prefix = ["nice", "-n", "19"] if nice and shutil.which("nice") else []
        if self.ffmpeg:
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            log.warning("ffmpeg not found: recordings are served unprocessed")

    @property
    def enabled(self) -> bool:
        return bool(self.ffmpeg)

    def get(self, key: str, data: bytes) -> bytes:
        """Processed version of `data` (cached under `key`), or `data` itself when processing isn't possible."""
        if not self.ffmpeg:
            return data
        path = self.cached_path(key)
        if path.exists():
            return path.read_bytes()
        try:
            out = self.process(data)
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log.warning("processing %s failed, serving the original: %s", key, e)
            return data
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(out)
        tmp.replace(path)
        return out

    def cached_path(self, key: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".mp3")

    def process(self, data: bytes) -> bytes:
        """Trim silence, measure, then apply one gain so the mean hits TARGET_MEAN_DB with peaks under the limit."""
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


class Warmer:
    """Pre-processes every recording in the background (lowest CPU priority), most-played sources first, so first
    plays don't wait for ffmpeg. Restarts resume: already-cached clips are skipped."""

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

        def one(row):
            member = f"{row[0]}_files/{row[1]}"
            if self.processor.cached_path(member).exists():
                self.skipped += 1
                return
            data = self.store.read(member)
            if data is None:
                self.failed += 1
                return
            self.processor.get(member, data)
            self.done += 1

        with ThreadPoolExecutor(self.workers, thread_name_prefix="warm") as pool:
            for start in range(0, len(rows), 256):          # feed the pool in batches: bounded memory
                list(pool.map(one, rows[start:start + 256]))
        self.finished = True
        log.info("warmer: finished (%d processed, %d already cached, %d missing)", self.done, self.skipped, self.failed)

    def status(self) -> dict:
        return {"total": self.total, "processed": self.done, "already_cached": self.skipped, "missing": self.failed,
                "finished": self.finished}
