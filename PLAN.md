# yobikoe: plan (proposal, 2026-11-15)

A self-hosted Japanese audio source for Yomitan: the recordings of the "Yomitan Ultimate Audio" pack, served from a
small Docker container on the home server, plus a pitch-accent-aware text-to-speech fallback using a local VOICEVOX
engine. Replaces the Patreon-hosted Cloudflare Worker. Public repo (MIT), no audio or index data in it.

## The audio pack (inspected 2026-11-15)

`yomitan_ultimate_audio_source.zip`, 5.5 GB (6.04 GB unpacked), seeding in `data/torrents`:

| Folder | Files | Size |
|---|---:|---:|
| jpod_files | 134,105 | 1.76 GB |
| nhk16_files | 102,825 | 1.39 GB |
| forvo_files | 78,841 | 0.75 GB |
| shinmeikai8_files | 66,729 | 0.64 GB |
| daijisen_files | 61,523 | 0.95 GB |
| taas_files | 19,441 | 0.12 GB |
| forvo_ext_files | 10,491 | 0.17 GB |
| ozk5_files | 9,098 | 0.11 GB |
| forvo_ext2_files | 2,240 | 0.03 GB |

`entry_and_pitch_db.sql` (118 MB, SQLite dump):
- `entries(id, expression, reading, source, speaker, display, file)`: 877,464 rows, e.g.
  `帯広 / おびひろ / nhk16 / display "オビヒロ [0]" / file "audio/20180222110605.mp3"`;
- `pitch_accents(id, expression, reading, pitch, count)`: 119,020 rows, pitch in Amazon Polly kana notation,
  e.g. `ナガ'ス` (apostrophe = the drop after that mora), `・` between accent phrases.

## Design

**One small service, `yobikoe`** (Python, FastAPI; same stack as mochibridge):

- `GET /audio/list?term=&reading=`: Yomitan's "Custom URL (JSON)" format (`{"type": "audioSourceList",
  "audioSources": [{"name", "url"}]}`), with the same matching as the worker: expression + reading, then expression
  only, then reading only (katakana readings folded to hiragana), sorted by match type then source
  (nhk16, daijisen, shinmeikai8, jpod, taas, ozk5, forvo, forvo_ext, forvo_ext2), TTS entries last.
  Optional `sources=` filter, as the worker has.
- `GET /audio/get/{source}/{file}`: the mp3.
- `GET /audio/tts?term=&reading=&pitch=`: generated speech (below), cached on disk.
- Index: on first start the SQL dump is imported into `/data/yobikoe.db` (SQLite) with its indexes; later starts reuse it.
- **Audio served straight from the zip**, read-only: Python's zip reader opens any member directly (the archive's
  directory is read once at startup), so the 485k files are never unpacked and the torrent keeps seeding.
- Optional API key (`?apiKey=`), off by default because the service is LAN/Tailscale-only.

**Text-to-speech: VOICEVOX** (free Japanese TTS engine, official Docker image, CPU build), as a second container:
- for each pitch pattern of the word in `pitch_accents` (most common first, as the worker does), yobikoe asks VOICEVOX
  for the reading's accent phrases, sets each phrase's accent position from the pitch notation
  (`ナガ'ス` -> drop after mora 2), synthesizes, converts to mp3 and caches it;
- words without pitch data get VOICEVOX's own accent estimate;
- the voice (speaker) is configurable; the README credits it as VOICEVOX's terms ask.

**Extras (phase 3, optional):** loudness normalisation (Forvo clips vary a lot) and silence trimming, applied once and
cached; configurable source order.

## Deployment (same conventions as the other stacks)

- Compose Manager project `yobikoe`: services `yobikoe` and `voicevox` (VOICEVOX only reachable by yobikoe on a
  private network), Traefik route `yobikoe.<domain>` with the LAN/Tailscale-only middlewares, no published ports.
- Appdata `appdata/yobikoe/`: `yobikoe.db` (~0.2 GB), `tts-cache/`. Audio: the zip, mounted read-only.
- Yomitan: Audio source "Custom URL (JSON)" -> `https://yobikoe.<domain>/audio/list?term={term}&reading={reading}`.

## Phases

1. **Recordings**: server, index import, zip-backed files, Yomitan setup on desktop and phone. Check against the
   worker on a sample of words (same entries, same order).
2. **TTS**: VOICEVOX container, pitch conversion, cache; listen to a few words with known accents
   (e.g. 箸 vs 橋 vs 端) next to the Polly versions.
3. **Extras**, public GitHub repo (README with credits: Yomitan Ultimate Audio, local-audio-yomichan, VOICEVOX + voice).

## Decisions needed

1. **Where the audio lives**: serve from the seeding zip in `data/torrents` (no extra space; it disappears if the
   torrent is ever removed), or copy the zip into `appdata/yobikoe/` (5.5 GB of the 158 GB free on the appdata SSD;
   independent of the torrent; faster on the SSD).
2. **API key**: off (LAN/Tailscale only, like mochibridge) or on?
3. **VOICEVOX voice**: pick later by ear (I'll prepare a few samples), or a neutral default?
