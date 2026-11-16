"""Pitch accent notation.

The pack's `pitch_accents.pitch` uses Amazon Polly's kana notation: katakana, `'` right after the mora where the
pitch drops (`ナガ'ス`, `ハシ'`), none for a flat word (`ナカズ`), `・` between accent phrases (`ナカズ・トバズ`).

VOICEVOX accepts almost the same notation (its "AquesTalk-like" kana) with two differences: phrases are separated
by `/`, and every phrase needs an accent mark. A phrase without a drop is flat (heiban); spoken on its own a flat
word sounds the same as one that drops after its last mora, so it gets `'` at the end.
"""

from __future__ import annotations

SMALL = set("ァィゥェォャュョヮ")
KATAKANA = {chr(c) for c in range(ord("ァ"), ord("ヺ") + 1)} | {"ー"}


def to_katakana(text: str) -> str:
    """Hiragana -> katakana; everything else unchanged."""
    return "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in text)


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def morae(kana: str) -> list[str]:
    """Split katakana into morae: small ャ/ュ/ョ/ァ... join the previous mora; ッ, ン, ー count on their own."""
    out: list[str] = []
    for ch in kana:
        if ch in SMALL and out:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def accent_positions(pitch: str) -> list[tuple[str, int]]:
    """`ナガ'ス・トバズ` -> [('ナガス', 2), ('トバズ', 0)]: each phrase's kana and its drop position (0 = flat)."""
    phrases = []
    for part in pitch.split("・"):
        kana = part.replace("'", "")
        drop = part.find("'")
        phrases.append((kana, len(morae(part[:drop])) if drop >= 0 else 0))
    return phrases


_VOWEL_ROWS = {
    "ア": "アカガサザタダナハバパマヤャラワァヵ", "イ": "イキギシジチヂニヒビピミリィヰ",
    "ウ": "ウクグスズツヅヌフブプムユュルゥヴ", "エ": "エケゲセゼテデネヘベペメレェヱヶ",
    "オ": "オコゴソゾトドノホボポモヨョロヲォ",
}
_VOWEL = {ch: v for v, chars in _VOWEL_ROWS.items() for ch in chars}


def spell_long_vowels(kana: str) -> str:
    """VOICEVOX's kana parser has no `ー`: write the vowel out (`ベンキョー` -> `ベンキョオ`, `トーキョー` -> `トオキョオ`)."""
    out = []
    for ch in kana:
        if ch == "ー" and out:
            prev = next((c for c in reversed(out) if c in _VOWEL), None)
            out.append(_VOWEL[prev] if prev else ch)
        else:
            out.append(ch)
    return "".join(out)


def voicevox_kana(pitch: str) -> str | None:
    """Polly notation -> VOICEVOX kana, or None when it holds characters VOICEVOX's kana parser won't take."""
    phrases = []
    for kana, drop in accent_positions(pitch):
        if not kana or any(ch not in KATAKANA for ch in kana):
            return None
        m = morae(spell_long_vowels(kana))           # ー -> vowel keeps the mora count, so `drop` still fits
        drop = drop or len(m)
        phrases.append("".join(m[:drop]) + "'" + "".join(m[drop:]))
    return "/".join(phrases)
