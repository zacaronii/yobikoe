"""Which recordings (and text-to-speech variants) to offer Yomitan for a term, in what order."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .pitch import to_hiragana, to_katakana, voicevox_kana

SOURCE_ORDER = ["nhk16", "daijisen", "shinmeikai8", "jpod", "taas", "ozk5", "forvo", "forvo_ext", "forvo_ext2"]
MATCH_LABELS = {0: "Expression+Reading", 1: "Only Expression", 2: "Only Reading"}


@dataclass(frozen=True)
class Recording:
    source: str
    file: str               # path inside "<source>_files/"
    name: str               # label shown in Yomitan
    match: int              # 0 expression+reading, 1 expression only, 2 reading only

    @property
    def member(self) -> str:
        return f"{self.source}_files/{self.file}"


def recordings(db: sqlite3.Connection, term: str, reading: str, sources: list[str] | None = None) -> list[Recording]:
    """Recordings for term/reading, best first: match type, then source order, then the pack's own order."""
    reading = to_hiragana(reading.strip()) if reading else ""
    where, params = "expression = ?", [term]
    if reading:
        where, params = "(expression = ? OR reading = ?)", [term, reading]
    if sources:
        where += f" AND source IN ({','.join('?' * len(sources))})"
        params += sources
    rows = db.execute(f"SELECT id, expression, reading, source, speaker, display, file FROM entries WHERE {where}",
                      params).fetchall()
    out = []
    for r in rows:
        if r["expression"] == term and reading and r["reading"] == reading:
            match = 0
        elif r["expression"] == term:
            match = 0 if not reading else 1
        else:
            match = 2
        label = r["source"] + (f": {r['display']}" if r["display"] else "")
        if reading:
            label += f" ({MATCH_LABELS[match]})"
        out.append((match, _rank(r["source"]), r["id"], Recording(r["source"], r["file"], label, match)))
    out.sort(key=lambda t: t[:3])
    return [t[3] for t in out]


def pitches(db: sqlite3.Connection, term: str, reading: str) -> list[str]:
    """Known pitch patterns for term (+ reading), most common first."""
    if reading:
        rows = db.execute("SELECT pitch FROM pitch_accents WHERE expression = ? AND reading = ? ORDER BY count DESC, id",
                          (term, to_hiragana(reading))).fetchall()
    else:
        rows = db.execute("SELECT pitch FROM pitch_accents WHERE expression = ? ORDER BY count DESC, id",
                          (term,)).fetchall()
    seen, out = set(), []
    for (p,) in rows:
        # Skip patterns TTS can't say, e.g. suffix entries written with a placeholder ("○○○'バシ" for 〜橋).
        if p not in seen and voicevox_kana(to_katakana(p)):
            seen.add(p)
            out.append(p)
    return out


def _rank(source: str) -> int:
    return SOURCE_ORDER.index(source) if source in SOURCE_ORDER else len(SOURCE_ORDER)
