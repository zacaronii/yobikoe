import io
import wave
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from yobikoe import lookup, pitch, store
from yobikoe.app import create_app
from yobikoe.tts import TTS

SQL = """PRAGMA defer_foreign_keys=TRUE;
CREATE TABLE IF NOT EXISTS "entries" (
    id INTEGER NOT NULL PRIMARY KEY, expression TEXT NOT NULL, reading TEXT, source TEXT NOT NULL, speaker TEXT,
    display TEXT, file TEXT NOT NULL, UNIQUE(expression, reading, source, file));
INSERT INTO entries VALUES(1,'橋','はし','forvo','spk1',NULL,'spk1/橋.mp3');
INSERT INTO entries VALUES(2,'橋','はし','nhk16',NULL,'ハシ＼ [2]','audio/hashi.mp3');
INSERT INTO entries VALUES(3,'箸','はし','nhk16',NULL,'ハ＼シ [1]','audio/hashi2.mp3');
INSERT INTO entries VALUES(4,'橋','はし','jpod',NULL,NULL,'はし - 橋.mp3');
CREATE TABLE pitch_accents (id INTEGER PRIMARY KEY AUTOINCREMENT, expression TEXT NOT NULL, reading TEXT NOT NULL,
    pitch TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1, UNIQUE(expression, reading, pitch));
INSERT INTO pitch_accents VALUES(1,'橋','はし','ハシ''',3);
INSERT INTO pitch_accents VALUES(2,'橋','はし','ハ''シ',1);
INSERT INTO pitch_accents VALUES(3,'橋','ばし','○○○''バシ',5);
CREATE INDEX idx_entries_expression ON entries(expression);
"""


@pytest.fixture
def pack(tmp_path):
    z = tmp_path / "audio.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        f.writestr("entry_and_pitch_db.sql", SQL)
        f.writestr("forvo_files/spk1/橋.mp3", b"FORVO" * 50)
        f.writestr("nhk16_files/audio/hashi.mp3", b"NHK-HASHI" * 50)
        f.writestr("nhk16_files/audio/hashi2.mp3", b"NHK-HASHI2" * 50)
        f.writestr("jpod_files/はし - 橋.mp3", b"JPOD" * 50, compress_type=zipfile.ZIP_STORED)
    return z, tmp_path / "yobikoe.db"


def test_build_reads_deflated_and_stored_files(pack):
    z, db = pack
    store.build(z, db)
    assert store.is_current(db, z)
    s = store.AudioStore(z, db)
    assert s.read("nhk16_files/audio/hashi.mp3") == b"NHK-HASHI" * 50
    assert s.read("jpod_files/はし - 橋.mp3") == b"JPOD" * 50
    assert s.read("nhk16_files/nope.mp3") is None


def test_order_match_type_then_source(pack):
    z, db = pack
    store.build(z, db)
    s = store.AudioStore(z, db)
    names = [(r.source, r.match) for r in lookup.recordings(s.db, "橋", "ハシ")]   # katakana reading folds to hiragana
    assert names == [("nhk16", 0), ("jpod", 0), ("forvo", 0), ("nhk16", 2)]     # 箸 shares only the reading
    assert lookup.pitches(s.db, "橋", "はし") == ["ハシ'", "ハ'シ"]                 # most common first
    assert lookup.pitches(s.db, "橋", "") == ["ハシ'", "ハ'シ"]                     # no placeholder patterns


@pytest.mark.parametrize("polly,expected", [
    ("ハシ'", "ハシ'"), ("ハ'シ", "ハ'シ"), ("ハシ", "ハシ'"),                      # flat words get the mark at the end
    ("ナガスク'ジラ", "ナガスク'ジラ"), ("ナカズ・トバズ", "ナカズ'/トバズ'"),
    ("キャ'ク", "キャ'ク"), ("トーキョー", "トオキョオ'"), ("ベンキョー", "ベンキョオ'"), ("コーヒー'", "コオヒイ'"),
    ("ラ'ーメン", "ラ'アメン"), ("abc", None),
])
def test_voicevox_kana(polly, expected):
    assert pitch.voicevox_kana(polly) == expected


def test_accent_positions_count_morae():
    assert pitch.accent_positions("キャ'クセキ・カ'イ") == [("キャクセキ", 1), ("カイ", 1)]


def _wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


def fake_engine(calls):
    def handler(request: httpx.Request):
        calls.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/audio_query":
            return httpx.Response(200, json={"accent_phrases": ["guess"], "speedScale": 1.0})
        if request.url.path == "/accent_phrases":
            return httpx.Response(200, json=["from-pitch"])
        if request.url.path == "/synthesis":
            return httpx.Response(200, content=_wav())
        return httpx.Response(404)
    return handler


def test_tts_uses_pitch_and_caches(tmp_path):
    calls = []
    t = TTS("http://voicevox", 13, tmp_path / "cache")
    t.client = httpx.Client(base_url="http://voicevox", transport=httpx.MockTransport(fake_engine(calls)))
    mp3 = t.speak("はし", "ハ'シ")
    assert mp3[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3") or mp3[0] == 0xFF
    assert ("/accent_phrases", {"text": "ハ'シ", "speaker": "13", "is_kana": "true"}) in calls
    n = len(calls)
    assert t.speak("はし", "ハ'シ") == mp3 and len(calls) == n             # second time from the cache


def test_api(pack, tmp_path):
    z, db = pack
    calls = []
    t = TTS("http://voicevox", 13, tmp_path / "cache")
    t.client = httpx.Client(base_url="http://voicevox", transport=httpx.MockTransport(fake_engine(calls)))
    c = TestClient(create_app(z, db, tts=t))
    body = c.get("/audio/list", params={"term": "橋", "reading": "はし"}).json()
    assert body["type"] == "audioSourceList"
    names = [a["name"] for a in body["audioSources"]]
    assert names[0] == "nhk16: ハシ＼ [2] (Expression+Reading)" and names[-2:] == ["TTS (voice 13): ハシ'", "TTS (voice 13): ハ'シ"]
    first = c.get(body["audioSources"][0]["url"].replace("http://testserver", ""))
    assert first.content == b"NHK-HASHI" * 50 and first.headers["content-type"] == "audio/mpeg"
    jpod = next(a["url"] for a in body["audioSources"] if a["name"].startswith("jpod"))
    assert c.get(jpod.replace("http://testserver", "")).content == b"JPOD" * 50       # spaces/kana in the path
    tts_url = body["audioSources"][-1]["url"].replace("http://testserver", "")
    assert c.get(tts_url).headers["content-type"] == "audio/mpeg"
    assert c.get("/audio/get/../../etc/passwd").status_code == 404


def test_api_key_is_optional(pack):
    z, db = pack
    open_ = TestClient(create_app(z, db))
    assert open_.get("/audio/list", params={"term": "橋"}).status_code == 200
    locked = TestClient(create_app(z, db, api_key="k"))
    assert locked.get("/audio/list", params={"term": "橋"}).status_code == 401
    body = locked.get("/audio/list", params={"term": "橋", "apiKey": "k"}).json()
    assert all("apiKey=k" in a["url"] for a in body["audioSources"])


def test_two_voices_in_order_and_only_enabled_ones(pack, tmp_path):
    z, db = pack
    calls = []
    t = TTS("http://voicevox", 16, tmp_path / "cache")
    t.client = httpx.Client(base_url="http://voicevox", transport=httpx.MockTransport(fake_engine(calls)))
    c = TestClient(create_app(z, db, tts=t, speakers=[16, 52]))
    tts = [a for a in c.get("/audio/list", params={"term": "橋", "reading": "はし"}).json()["audioSources"]
           if a["name"].startswith("TTS")]
    assert [a["name"] for a in tts] == ["TTS (voice 16): ハシ'", "TTS (voice 16): ハ'シ",
                                        "TTS (voice 52): ハシ'", "TTS (voice 52): ハ'シ"]
    assert "speaker=52" in tts[2]["url"]
    assert c.get("/audio/tts", params={"term": "橋", "reading": "はし", "speaker": 3}).status_code == 404


class FakeProcessor:
    enabled = False
    def __init__(self): self.seen = []
    def get(self, key, data): self.seen.append(key); return b"LEVELLED:" + data[:5]


def test_recordings_are_processed_unless_raw(pack):
    z, db = pack
    p = FakeProcessor()
    c = TestClient(create_app(z, db, processor=p))
    assert c.get("/audio/get/nhk16/audio/hashi.mp3").content == b"LEVELLED:NHK-H"
    assert c.get("/audio/get/nhk16/audio/hashi.mp3", params={"raw": 1}).content == b"NHK-HASHI" * 50
    assert p.seen == ["nhk16_files/audio/hashi.mp3"]


def test_processor_falls_back_to_the_original(tmp_path):
    from yobikoe.process import Processor
    broken = Processor(tmp_path / "pc", ffmpeg="/bin/false")         # "ffmpeg" that always fails
    assert broken.get("k", b"ORIGINAL") == b"ORIGINAL"
    assert not broken.cache_dir.joinpath("x").exists()


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="needs ffmpeg")
def test_processor_trims_and_levels_real_audio(tmp_path):
    import subprocess
    from yobikoe.process import Processor
    # 0.5 s silence + 0.5 s quiet tone + 0.5 s silence, as mp3
    src = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                          "aevalsrc='if(between(t,0.5,1),0.05*sin(2*PI*440*t),0)':d=1.5:s=24000",
                          "-f", "mp3", "-"], capture_output=True, check=True).stdout
    out = Processor(tmp_path / "pc").get("tone", src)
    def duration_and_mean(b):
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", "pipe:0", "-af", "volumedetect", "-f", "null", "-"],
                           input=b, capture_output=True).stderr.decode()
        import re
        mean = float(re.search(r"mean_volume: (-?[\d.]+)", r).group(1))
        t = re.findall(r"time=(\d+):(\d+):([\d.]+)", r)[-1]
        return int(t[0]) * 3600 + int(t[1]) * 60 + float(t[2]), mean
    (d0, m0), (d1, m1) = duration_and_mean(src), duration_and_mean(out)
    assert d1 < d0 - 0.6                       # most of the leading/trailing silence is gone
    assert abs(m1 - (-20)) < 3 and m1 > m0     # louder, close to the target level


def test_warmer_processes_most_played_sources_first_and_resumes(pack, tmp_path):
    from yobikoe.process import Processor, Warmer
    z, db = pack
    store.build(z, db)
    s = store.AudioStore(z, db)

    class Recording(Processor):
        def __init__(self): super().__init__(tmp_path / "pc", ffmpeg="ffmpeg-stub"); self.order = []
        def process(self, data): self.order.append(data[:4]); return b"P"

    p = Recording()
    w = Warmer(s, p, lookup.SOURCE_ORDER, workers=1)
    w._run()
    assert w.status()["processed"] == 4 and p.order[0][:3] == b"NHK" and p.order[-1] == b"FORV"
    again = Warmer(s, p, lookup.SOURCE_ORDER, workers=1)
    again._run()
    assert again.status()["processed"] == 0 and again.status()["already_cached"] == 4
