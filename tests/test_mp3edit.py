"""Lossless mp3 editing and the levelling built on it, against real mp3s made with ffmpeg."""
import re
import shutil
import subprocess
import tempfile

import pytest

from yobikoe import mp3edit
from yobikoe.process import LevelStore, Levels, Processor, gain_steps, parse_analysis

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
TONE = "aevalsrc='if(between(t,0.5,1),{amp}*sin(2*PI*440*t),0)':d=1.5:s={rate}"


def mp3(rate=48000, channels=1, amp=0.05):
    """0.5 s silence + 0.5 s tone + 0.5 s silence; ffmpeg's encoder writes an Info frame with a LAME tag like the pack."""
    # To a file, not a pipe: ffmpeg writes the Info frame by seeking back once encoding is done.
    with tempfile.TemporaryDirectory() as d:
        path = f"{d}/t.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", TONE.format(amp=amp, rate=rate),
                        "-ac", str(channels), path], check=True)
        return open(path, "rb").read()


def decode(b):
    """(seconds, mean dB, error lines) as ffmpeg decodes it. From a file: through a pipe ffmpeg can't compare the
    Info frame's byte count with the file size and warns about every file."""
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/in.mp3", "wb").write(b)
        r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", f"{d}/in.mp3", "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True).stderr.decode()
    h, m, sec = re.findall(r"time=(\d+):(\d+):([\d.]+)", r)[-1]
    mean = float(re.search(r"mean_volume: (-?[\d.]+)", r).group(1))
    errors = [l for l in r.splitlines() if re.search(r"error|invalid|overread|corrupt|mismatch", l, re.I)]
    return int(h) * 3600 + int(m) * 60 + float(sec), mean, errors


def lame_crcs_ok(b):
    m = mp3edit.parse(b)
    o, info = m.lame, m.info
    music = b[m.frames[0].offset:m.frames[-1].offset + m.frames[-1].length]
    head = bytearray(b[info.offset:info.offset + min(190, info.length)].ljust(190, b"\0"))
    at = o - info.offset + 34
    head[at:min(at + 2, 190)] = bytes(max(0, min(2, 190 - at)))       # the CRC field counts as zero (if inside)
    return (int.from_bytes(b[o + 32:o + 34], "big") == mp3edit.crc16(music),
            int.from_bytes(b[o + 34:o + 36], "big") == mp3edit.crc16(bytes(head)),
            int.from_bytes(b[o + 28:o + 32], "big") == len(music) + info.length)


FORMATS = [(48000, 1), (44100, 2), (24000, 1)]          # MPEG-1 mono, MPEG-1 stereo, MPEG-2


@needs_ffmpeg
@pytest.mark.parametrize("rate,channels", FORMATS)
def test_no_op_edit_is_byte_identical(rate, channels):
    b = mp3(rate, channels)
    assert mp3edit.parse(b).info is not None and mp3edit.parse(b).lame is not None
    assert mp3edit.edit(b) == b
    assert lame_crcs_ok(b) == (True, True, True)             # our CRCs reproduce the encoder's


@needs_ffmpeg
@pytest.mark.parametrize("rate,channels", FORMATS)
def test_gain_steps_change_loudness_by_1_5_db_each(rate, channels):
    b = mp3(rate, channels)
    _, base, _ = decode(b)
    for steps in (4, -4):
        out = mp3edit.edit(b, steps=steps)
        _, mean, errors = decode(out)
        assert abs((mean - base) - steps * mp3edit.STEP_DB) < 0.3 and not errors
        assert lame_crcs_ok(out) == (True, True, True)


@needs_ffmpeg
@pytest.mark.parametrize("rate,channels", FORMATS)
def test_trim_drops_whole_frames_and_rewrites_the_info_frame(rate, channels):
    b = mp3(rate, channels)
    before = mp3edit.parse(b)
    out = mp3edit.edit(b, head=5, tail=5)
    after = mp3edit.parse(out)
    assert len(after.frames) == len(before.frames) - 10
    x = after.info.side_offset + after.info.side_length
    assert int.from_bytes(out[x + 8:x + 12], "big") == len(after.frames)              # Xing frame count
    assert lame_crcs_ok(out) == (True, True, True)
    (d0, _, _), (d1, _, errors) = decode(b), decode(out)
    assert not errors
    spf = before.first.samples
    assert abs((d0 - d1) - 10 * spf / rate) < 1.5 * spf / rate                        # ~10 frames shorter


def test_garbage_is_refused():
    with pytest.raises(mp3edit.Mp3Error):
        mp3edit.edit(b"RIFF....WAVEfmt " + bytes(100))


def test_gain_steps_respect_the_peak_limit():
    assert gain_steps(-30.0, -20.0) == 7                 # +10 dB wanted: ~6.7 steps -> 7, peak stays far below -1
    assert gain_steps(-30.0, -4.0) == 1                  # peak at -4: only one step fits under -1 dB
    assert gain_steps(-12.0, -0.5) == -5                 # too loud: brought down


def test_parse_analysis():
    stats = """[astats@all @ 0x1] Overall
[astats@all @ 0x1] Peak level dB: 2.037307
[astats@all @ 0x1] Number of samples: 133679
[Parsed_volumedetect_7 @ 0x2] mean_volume: -18.4 dB
[Parsed_volumedetect_7 @ 0x2] max_volume: 0.0 dB
[astats@kept @ 0x3] Peak level dB: 2.037307
[astats@kept @ 0x3] Number of samples: 63031
[astats@lead @ 0x4] Number of samples: 116616
"""
    a = parse_analysis(stats)
    assert (a.total, a.lead, a.trail, a.mean, a.peak) == (133679, 17063, 53585, -18.4, 2.037307)
    assert parse_analysis(stats.replace("63031", "0")) is None                 # all silence
    assert parse_analysis("nothing") is None


@needs_ffmpeg
@pytest.mark.parametrize("rate,channels", FORMATS)
def test_processor_measures_once_then_edits_losslessly(tmp_path, rate, channels):
    b = mp3(rate, channels)

    class Counting(Processor):
        runs = 0
        def analyse(self, data):
            Counting.runs += 1
            return super().analyse(data)

    p = Counting(LevelStore(tmp_path / "levels.db"))
    out = p.get("k", b)
    assert p.get("k", b) == out and Counting.runs == 1                # measured once, stored
    lv = p.levels.get("k")
    assert lv.head > 0 and lv.tail > 0 and lv.steps > 0
    (d0, m0, _), (d1, m1, errors) = decode(b), decode(out)
    assert not errors
    assert d1 < d0 - 0.75                                               # most of the 1 s of silence is gone
    assert m1 > m0 + 10 and abs(m1 - (-20)) < 2.5                       # louder, near the target
    assert len(mp3edit.parse(out).frames) == len(mp3edit.parse(b).frames) - lv.head - lv.tail


@needs_ffmpeg
def test_processor_re_encodes_what_it_cannot_edit(tmp_path):
    wav = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", TONE.format(amp=0.05, rate=24000),
                          "-f", "wav", "-"], capture_output=True, check=True).stdout
    out = Processor(LevelStore(tmp_path / "levels.db")).get("w", wav)
    assert mp3edit.parse(out).frames                                    # came back as a (re-encoded) mp3


def test_levels_are_dropped_when_the_pack_changes(tmp_path):
    a = LevelStore(tmp_path / "levels.db", "pack-1")
    a.put("m", Levels(1, 2, 3))
    assert LevelStore(tmp_path / "levels.db", "pack-1").get("m") == Levels(1, 2, 3)
    assert LevelStore(tmp_path / "levels.db", "pack-2").get("m") is None
