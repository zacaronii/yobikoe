"""HTTP API for Yomitan's "Custom URL (JSON)" audio source.

    GET /audio/list?term=&reading=[&sources=a,b][&apiKey=]   -> {"type": "audioSourceList", "audioSources": [...]}
    GET /audio/get/{source}/{file}[?raw=1]                    -> the recording (mp3), trimmed and levelled
    GET /audio/tts?term=&reading=&pitch=&speaker=             -> generated speech (mp3)
    GET /health
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from . import lookup, store
from .process import Processor, Warmer
from .tts import TTS

log = logging.getLogger("yobikoe")

AUDIO_CACHE = "public, max-age=31536000, immutable"   # recordings never change for a given URL
TTS_CACHE = "public, max-age=86400"                    # same URL, but the voice is configurable


def create_app(zip_path: Path, db_path: Path, api_key: str | None = None, tts: TTS | None = None,
               speakers: list[int] | None = None, processor: Processor | None = None,
               public_url: str | None = None, warm_workers: int = 0) -> FastAPI:
    if not store.is_current(db_path, zip_path):
        store.build(zip_path, db_path)
    audio = store.AudioStore(zip_path, db_path)
    speakers = speakers or ([tts.speaker] if tts else [])
    voice_names: dict[int, str] = {}

    def voice_name(sid: int) -> str:
        """The engine's name for a speaker id (looked up once), e.g. 九州そら."""
        if not voice_names and tts is not None:
            try:
                for sp in tts.speakers():
                    for st in sp.get("styles", []):
                        voice_names[st["id"]] = sp["name"] if len(sp["styles"]) == 1 or st["name"] == "ノーマル" \
                            else f"{sp['name']} ({st['name']})"
            except Exception as e:                   # engine not up yet: label by id, retry next time
                log.info("voice names unavailable: %s", e)
        return voice_names.get(sid, f"voice {sid}")
    app = FastAPI(title="yobikoe", docs_url=None, redoc_url=None)

    def check_key(request: Request) -> None:
        if api_key and not secrets.compare_digest(request.query_params.get("apiKey", ""), api_key):
            raise HTTPException(401, "missing or wrong apiKey")

    def base(request: Request) -> str:
        return (public_url or str(request.base_url)).rstrip("/")

    def keyed(url: str) -> str:
        return url + (("&" if "?" in url else "?") + urlencode({"apiKey": api_key}) if api_key else "")

    @app.get("/voices", response_class=HTMLResponse)
    def voices():
        """Optional voice comparison page (samples/make_samples.py writes it next to the database)."""
        page = db_path.parent / "voices.html"
        if not page.exists():
            raise HTTPException(404, "no voice samples page")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    warmer = None
    if processor is not None and processor.enabled and warm_workers > 0:
        nice = Processor(processor.cache_dir, ffmpeg=processor.ffmpeg, nice=True)
        warmer = Warmer(audio, nice, lookup.SOURCE_ORDER, warm_workers)
        warmer.start()

    @app.get("/health")
    def health():
        return {"ok": True, "tts": tts is not None, "voices": speakers,
                "processing": processor is not None and processor.enabled,
                "preprocessing": warmer.status() if warmer else None}

    @app.get("/audio/list")
    def audio_list(request: Request, term: str = Query(..., min_length=1), reading: str = "", sources: str = ""):
        check_key(request)
        wanted = [s for s in sources.split(",") if s and s != "all"] or None
        root = base(request)
        items = [{"name": r.name, "url": keyed(f"{root}/audio/get/{r.source}/{quote(r.file)}")}
                 for r in lookup.recordings(audio.db, term, reading, wanted)]
        if tts is not None and (wanted is None or "tts" in wanted):
            patterns = lookup.pitches(audio.db, term, reading) or [""]
            for sid in speakers:
                for pitch in patterns:
                    params = urlencode({"term": term, "reading": reading or term, "pitch": pitch, "speaker": sid})
                    items.append({"name": f"TTS ({voice_name(sid)})" + (f": {pitch}" if pitch else ""),
                                  "url": keyed(f"{root}/audio/tts?{params}")})
        return {"type": "audioSourceList", "audioSources": items}

    @app.get("/audio/get/{source}/{file:path}")
    def audio_get(request: Request, source: str, file: str, raw: bool = False):
        check_key(request)
        if source not in lookup.SOURCE_ORDER:
            raise HTTPException(404, "unknown source")
        member = f"{source}_files/{file}"
        data = audio.read(member)
        if data is None:
            raise HTTPException(404, "no such recording")
        if processor is not None and not raw:
            data = processor.get(member, data)
        return Response(data, media_type="audio/mpeg", headers={"Cache-Control": AUDIO_CACHE})

    @app.get("/audio/tts")
    def audio_tts(request: Request, term: str = Query(..., min_length=1), reading: str = "", pitch: str = "",
                  speaker: int | None = None):
        check_key(request)
        if tts is None:
            raise HTTPException(404, "text-to-speech is not configured")
        sid = speakers[0] if speaker is None else speaker
        if sid not in speakers:
            raise HTTPException(404, "voice not enabled")
        try:
            data = tts.speak(reading or term, pitch, speaker=sid)
            if processor is not None:
                data = processor.get(f"tts\t{sid}\t{reading or term}\t{pitch}", data)
        except Exception as e:                       # engine down or rejected the text
            log.warning("tts failed for %r/%r: %s", term, pitch, e)
            raise HTTPException(503, "text-to-speech unavailable") from None
        return Response(data, media_type="audio/mpeg", headers={"Cache-Control": TTS_CACHE})

    app.state.audio = audio
    return app


def from_env() -> FastAPI:
    """The container's app: paths and options from the environment."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data = Path(os.environ.get("YOBIKOE_DATA", "/data"))
    engine = os.environ.get("VOICEVOX_URL", "").strip()
    # VOICEVOX_SPEAKERS: comma-separated speaker ids, first = default (VOICEVOX_SPEAKER kept for one voice).
    speakers = [int(s) for s in os.environ.get("VOICEVOX_SPEAKERS", os.environ.get("VOICEVOX_SPEAKER", "16")).split(",")
                if s.strip()]
    tts = TTS(engine, speakers[0], data / "tts-cache") if engine else None
    normalize = os.environ.get("YOBIKOE_NORMALIZE", "true").lower() not in ("0", "false", "no")
    processor = Processor(data / "processed-cache") if normalize else None
    return create_app(Path(os.environ.get("YOBIKOE_ZIP", str(data / "audio.zip"))),
                      Path(os.environ.get("YOBIKOE_DB", str(data / "yobikoe.db"))),
                      api_key=os.environ.get("YOBIKOE_API_KEY") or None, tts=tts, speakers=speakers, processor=processor,
                      public_url=os.environ.get("YOBIKOE_PUBLIC_URL") or None,
                      warm_workers=int(os.environ.get("YOBIKOE_PREPROCESS_WORKERS", "2")) if normalize else 0)
