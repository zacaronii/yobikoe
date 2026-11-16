"""Pitch-accent text-to-speech through a VOICEVOX engine, cached as mp3 files."""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import wave
from pathlib import Path

import httpx
import lameenc

from .pitch import to_katakana, voicevox_kana

log = logging.getLogger(__name__)


class TTS:
    def __init__(self, engine_url: str, speaker: int, cache_dir: Path, timeout: float = 30.0):
        self.engine_url, self.speaker, self.cache_dir = engine_url.rstrip("/"), speaker, cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(base_url=self.engine_url, timeout=timeout)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def cache_path(self, reading: str, pitch: str, speaker: int | None = None) -> Path:
        key = f"{speaker if speaker is not None else self.speaker}\t{reading}\t{pitch}"
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".mp3")

    def speak(self, reading: str, pitch: str = "", speaker: int | None = None) -> bytes:
        """mp3 of `reading` spoken with `pitch` (pack notation, '' = let VOICEVOX guess). Cached by all three."""
        speaker = self.speaker if speaker is None else speaker
        path = self.cache_path(reading, pitch, speaker)
        if path.exists():
            return path.read_bytes()
        with self._lock_for(path.name):           # one synthesis per variant, even with concurrent requests
            if path.exists():
                return path.read_bytes()
            mp3 = _to_mp3(self._synthesize(reading, pitch, speaker))
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(mp3)
            tmp.replace(path)
            return mp3

    def _synthesize(self, reading: str, pitch: str, speaker: int) -> bytes:
        text = to_katakana(reading)
        query = self.client.post("/audio_query", params={"text": text, "speaker": speaker}).raise_for_status().json()
        kana = voicevox_kana(to_katakana(pitch)) if pitch else None
        if kana:
            try:
                query["accent_phrases"] = self.client.post(
                    "/accent_phrases", params={"text": kana, "speaker": speaker, "is_kana": "true"}
                ).raise_for_status().json()
            except httpx.HTTPStatusError as e:      # unusual kana: keep VOICEVOX's own accent guess
                log.warning("VOICEVOX rejected kana %r for %r: %s", kana, reading, e.response.text[:200])
        query["prePhonemeLength"] = 0.05
        query["postPhonemeLength"] = 0.1
        return self.client.post("/synthesis", params={"speaker": speaker}, json=query).raise_for_status().content

    def speakers(self) -> list[dict]:
        return self.client.get("/speakers").raise_for_status().json()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())


def _to_mp3(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        channels, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit audio from VOICEVOX, got {width * 8}-bit")
    enc = lameenc.Encoder()
    enc.set_bit_rate(96)
    enc.set_in_sample_rate(rate)
    enc.set_channels(channels)
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush())
