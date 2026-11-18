# <img src="art/kaeru.svg" alt="" width="54" height="42" align="absmiddle"> yobikoe

**yobikoe** (呼び声, "a call") is a self-hosted Japanese audio source for [Yomitan](https://github.com/yomidevs/yomitan).
It serves pronunciation recordings from the "Yomitan Ultimate Audio" pack. When a word has no recording, it falls back
to **pitch-accent-aware text-to-speech** from a local [VOICEVOX](https://voicevox.hiroshiba.jp/) engine.

It runs as two small Docker containers on your own machine or home server. There's no cloud account to set up, no Anki
to keep open, and nothing to pay for.

- **Fast:** the index is local SQLite, and recordings are read straight out of the pack's zip (no unpacking 485,000
  files). A lookup takes well under a millisecond on the server.
- **The same results Yomitan users are used to:** exact word + reading first, then word only, then reading only. Within
  each, the sources are ordered NHK, Daijisen, Shinmeikai, JapanesePod101, TAAS, OZK, Forvo.
- **Text-to-speech that follows the pitch accent:** the pack includes pitch accent data (`ハ'シ`, `ハシ`). yobikoe tells
  VOICEVOX where the pitch drops, so 箸 and 端 sound different. Each variant is generated once (about 0.1 s) and cached.
- **Even recordings:** clips are trimmed of leading/trailing silence and brought to the same loudness, so a quiet
  Forvo recording doesn't follow a loud NHK one. A background job processes the whole pack at low priority, so first
  plays don't wait. The pack itself is never modified, and `?raw=1` returns any recording untouched.

## Audio pack (bring your own)

The recordings and their index aren't part of this repository. yobikoe expects the "Yomitan Ultimate Audio" pack
(`yomitan_ultimate_audio_source.zip`, about 5.5 GB). It contains folders like `nhk16_files/`, `jpod_files/` and
`forvo_files/`, plus `entry_and_pitch_db.sql`. See the
[Yomitan Ultimate Audio](https://github.com/friedrich-de/yomitan-ultimate-audio) project for where to get it.

Put the zip at `<data>/audio.zip`. On first start, yobikoe builds its index (`<data>/yobikoe.db`, about 340 MB) in
roughly 10–20 seconds. If the zip ever changes, the index is rebuilt automatically.
Use the zip exactly as downloaded: don't extract it or re-zip it. If it's the only zip in the folder, yobikoe finds it
under any name (e.g. still `yomitan_ultimate_audio_source.zip`).

## What to expect

- **Disk:** about 11 GB in total: the pack (5.5 GB), the index (0.3 GB) and levelled copies of the recordings (about
  5.2 GB). Set `YOBIKOE_NORMALIZE=false` to skip the levelled copies and serve the recordings as they are.
- **Memory:** roughly 300–600 MB for yobikoe and 100–250 MB for VOICEVOX.
- **First start:** the index takes 10–20 seconds to build. After that, yobikoe levels every recording in the background
  at the lowest CPU priority. That takes several hours (about 7 on a home server with 2 workers), and yobikoe works
  normally the whole time.
- **Platforms:** the image is built for x86-64 (Intel/AMD) only. If you need ARM (Apple Silicon, Raspberry Pi), open an
  issue.

## Run it

```yaml
# docker-compose.yml (see deploy/docker-compose.yml)
services:
  yobikoe:
    image: ghcr.io/zacaronii/yobikoe:latest
    user: "1000:1000"
    environment:
      VOICEVOX_URL: http://voicevox:50021
      VOICEVOX_SPEAKERS: "16,52"     # voices, first = default (see below)
    volumes:
      - ./data:/data                 # audio.zip goes here
    ports:
      - "8080:8080"
    networks: [default, voicevox]
  voicevox:
    image: voicevox/voicevox_engine:cpu-0.26.2
    networks: [voicevox]
networks:
  voicevox:
    internal: true                   # the engine needs no internet and only yobikoe talks to it
```

On Linux, set `user:` to your own ids (`id -u` and `id -g`), and create `data/` yourself before starting so you own it.
With Docker Desktop on Windows or Mac, leave it as is.

Then, in Yomitan: **Settings → Audio → Configure audio playback sources → Custom URL (JSON)**:

```
http://<host>:8080/audio/list?term={term}&reading={reading}
```

`<host>` is `localhost` if Docker runs on the same computer as your browser, otherwise your server's LAN address.

The server has no login. Keep it on your LAN or VPN, or set `YOBIKOE_API_KEY` and add `&apiKey=<key>` to that URL.

### Settings

| Variable | Default | |
|---|---|---|
| `VOICEVOX_URL` | (unset: no TTS) | The engine, e.g. `http://voicevox:50021` |
| `VOICEVOX_SPEAKERS` | `16` | Comma-separated VOICEVOX speaker ids; each becomes a "TTS (voice)" entry, the first is the default |
| `YOBIKOE_NORMALIZE` | `true` | Trim silence and level recordings |
| `YOBIKOE_PREPROCESS_WORKERS` | `2` | Background processing threads (0 = only process on first play) |
| `YOBIKOE_API_KEY` | (unset) | Require `?apiKey=` on every request |
| `YOBIKOE_PUBLIC_URL` | (from the request) | Base URL for links, if the proxy headers aren't enough |
| `YOBIKOE_DATA` | `/data` | Where `audio.zip`, the index and caches live |

### Voices

The engine's voices are listed at `http://voicevox:50021/speakers`. Calm, neutral voices work best for dictionary
pronunciations, for example 九州そら (16), 雀松朱司 (52), No.7 アナウンス (30) or 冥鳴ひまり (14).
`samples/make_samples.py` writes a comparison page, served at `/voices`, with the NHK recording next to each voice for a
few minimal pairs (箸/端, 雨/飴...). Don't publish that page: it embeds recordings from the pack.

VOICEVOX's terms ask you to credit the voice when you publish audio made with it (e.g. "VOICEVOX:九州そら"). Using it
for your own study needs nothing.

## Troubleshooting

If the container stops right after starting, its log (`docker logs yobikoe`) ends with one line saying why: no pack
found, a file that isn't the pack, or a data folder it can't write to.

If Yomitan shows no yobikoe entries, open `http://<host>:8080/health` in the browser that runs Yomitan. If that doesn't
load, the address or port is wrong, or something is blocking it.

## API

| Endpoint | |
|---|---|
| `GET /audio/list?term=&reading=[&sources=nhk16,forvo,tts]` | Yomitan's `audioSourceList` JSON |
| `GET /audio/get/{source}/{file}[?raw=1]` | A recording (mp3), levelled unless `raw` |
| `GET /audio/tts?term=&reading=&pitch=&speaker=` | Generated speech (mp3) |
| `GET /health` | Status, including background processing progress |

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The tests build a tiny synthetic pack, so you don't need the real one. The ffmpeg test runs when ffmpeg is installed.

## Thanks

- **[Yomitan Ultimate Audio](https://github.com/friedrich-de/yomitan-ultimate-audio)** by friedrich-de: the audio pack,
  its index format and the idea of pitch-accent text-to-speech for Yomitan. yobikoe is an independent implementation
  that reads the same pack.
- **[local-audio-yomichan](https://github.com/yomidevs/local-audio-yomichan)**: the original local audio server for
  Yomichan/Yomitan and the source collections behind it.
- **[VOICEVOX](https://voicevox.hiroshiba.jp/)** and its voice providers, for a free, high-quality Japanese speech engine.
- **[Yomitan](https://github.com/yomidevs/yomitan)**.

yobikoe contains no audio. The recordings belong to their respective publishers and contributors.

## License

[MIT](LICENSE)
