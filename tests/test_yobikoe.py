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
    assert names[0] == "nhk16: ハシ＼ [2] (Expression+Reading)" and names[-2:] == ["TTS: ハシ'", "TTS: ハ'シ"]
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
