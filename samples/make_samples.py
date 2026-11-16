"""Builds samples/voices.html: VOICEVOX voices saying words with the pack's pitch accents, next to NHK recordings."""
import base64, html, sqlite3, sys
from pathlib import Path
from yobikoe.store import AudioStore
from yobikoe.tts import TTS
from yobikoe import lookup

VOICES = [(30, "No.7 (アナウンス)"), (29, "No.7 (ノーマル)"), (14, "冥鳴ひまり"), (10, "雨晴はう"), (16, "九州そら"),
          (13, "青山龍星"), (11, "玄野武宏"), (52, "雀松朱司")]
WORDS = [("箸", "はし"), ("端", "はし"), ("雨", "あめ"), ("飴", "あめ"), ("図書館", "としょかん"), ("美味しい", "おいしい"),
         ("冷蔵庫", "れいぞうこ"), ("勉強", "べんきょう")]

store = AudioStore(Path("/data/audio.zip"), Path("/data/yobikoe.db"))
tts = TTS(sys.argv[1], 30, Path(sys.argv[2]) / "tts-cache")
b64 = lambda data: "data:audio/mpeg;base64," + base64.b64encode(data).decode()
rows = []
for term, reading in WORDS:
    pitches = lookup.pitches(store.db, term, reading)
    pitch = pitches[0] if pitches else ""
    nhk = next((r for r in lookup.recordings(store.db, term, reading, ["nhk16"]) if r.match == 0), None)
    cells = [f"<td><b>{term}</b><br><small>{reading} · {html.escape(pitch) or 'no pitch data'}</small></td>",
             f"<td><audio controls preload='none' src='{b64(store.read(nhk.member))}'></audio></td>" if nhk else "<td>–</td>"]
    for sid, _ in VOICES:
        cells.append(f"<td><audio controls preload='none' src='{b64(tts.speak(reading, pitch, speaker=sid))}'></audio></td>")
    rows.append("<tr>" + "".join(cells) + "</tr>")
head = "<tr><th>Word</th><th>NHK recording</th>" + "".join(f"<th>{html.escape(n)}</th>" for _, n in VOICES) + "</tr>"
page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>yobikoe voice samples</title><style>
:root{{--bg:#fbfaf7;--fg:#222;--muted:#666;--line:#e3e0d8}}
@media (prefers-color-scheme: dark){{:root:not([data-theme="light"]){{--bg:#161615;--fg:#eee;--muted:#aaa;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:16px}}
table{{border-collapse:collapse}} th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:middle}}
th{{position:sticky;top:0;background:var(--bg);font-size:13px}} small{{color:var(--muted)}} audio{{width:150px;height:32px}}
.wrap{{overflow-x:auto}}</style></head><body>
<h1>yobikoe voice samples</h1>
<p>VOICEVOX 0.26.2 voices saying each word with the pitch accent from the audio pack's index (shown under the word;
<code>'</code> = where the pitch drops). The NHK recording is the reference. 箸/端 and 雨/飴 differ only in accent.</p>
<div class="wrap"><table>{head}{''.join(rows)}</table></div>
<p><small>Voices: VOICEVOX:No.7, 冥鳴ひまり, 雨晴はう, 九州そら, 青山龍星, 玄野武宏, 雀松朱司.</small></p></body></html>"""
Path(sys.argv[2], "voices.html").write_text(page, encoding="utf-8")
print("ok", len(page) // 1024, "KB")
