"""Lossless mp3 edits: change the volume and drop whole frames from either end, without decoding.

Volume: every Layer III granule carries an 8-bit `global_gain`; one step is 2^(1/4) in amplitude (~1.5 dB), so
adding n to every granule's value changes the loudness by n steps (the trick mp3gain uses). Trimming removes whole
frames (1152 samples in MPEG-1, 576 in MPEG-2/2.5).

The first frame is usually an Info/Xing frame (no audio; frame count, byte count, seek table and the LAME tag with
encoder delay/padding and two CRCs). After trimming those are rewritten so players see the right length.
Anything unexpected raises `Mp3Error`; the caller then falls back to decoding and re-encoding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_BITRATES = {
    True: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),   # MPEG-1, Layer III
    False: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),     # MPEG-2/2.5, Layer III
}
_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
STEP_DB = 1.505                      # 20*log10(2**0.25): one global_gain step
DECODER_DELAY = 529                  # samples every Layer III decoder adds (skipped together with the encoder delay)


class Mp3Error(ValueError):
    pass


@dataclass(frozen=True)
class Frame:
    offset: int
    length: int
    mpeg1: bool
    sample_rate: int
    mono: bool
    crc: bool

    @property
    def samples(self) -> int:
        return 1152 if self.mpeg1 else 576

    @property
    def side_offset(self) -> int:
        return self.offset + 4 + (2 if self.crc else 0)

    @property
    def side_length(self) -> int:
        return (17 if self.mono else 32) if self.mpeg1 else (9 if self.mono else 17)


def _frame(b: bytes, i: int) -> Frame | None:
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    h = int.from_bytes(b[i:i + 4], "big")
    version, layer, protection = (h >> 19) & 3, (h >> 17) & 3, (h >> 16) & 1
    bitrate, rate, padding, mode = (h >> 12) & 15, (h >> 10) & 3, (h >> 9) & 1, (h >> 6) & 3
    if version == 1 or rate == 3 or bitrate == 15:
        return None
    if layer != 1:
        raise Mp3Error("not Layer III")
    if bitrate == 0:
        raise Mp3Error("free-format bitrate")
    mpeg1 = version == 3
    sample_rate = _SAMPLE_RATES[version][rate]
    length = (144 if mpeg1 else 72) * _BITRATES[mpeg1][bitrate] * 1000 // sample_rate + padding
    return Frame(i, length, mpeg1, sample_rate, mode == 3, not protection)


@dataclass
class Mp3:
    data: bytes
    frames: list[Frame]          # audio frames (the Info frame excluded)
    info: Frame | None           # the Xing/Info frame, if any
    tail: bytes                  # whatever follows the last frame (ID3v1...)

    @property
    def first(self) -> Frame:
        return self.frames[0]

    @property
    def lame(self) -> int | None:
        """Offset of the LAME tag (in `data`), if the Info frame has one."""
        if self.info is None:
            return None
        x = self.info.side_offset + self.info.side_length
        flags = int.from_bytes(self.data[x + 4:x + 8], "big")
        o = x + 8 + 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
        tag = self.data[o:o + 36]
        if len(tag) < 36 or not tag[:4].isalpha() or o + 36 > self.info.offset + self.info.length:
            return None
        return o

    def delay_padding(self) -> tuple[int, int]:
        """Encoder delay and end padding in samples, from the LAME tag (0, 0 without one)."""
        o = self.lame
        if o is None:
            return 0, 0
        d = self.data
        return (d[o + 21] << 4) | (d[o + 22] >> 4), ((d[o + 22] & 15) << 8) | d[o + 23]


def parse(data: bytes) -> Mp3:
    i = 0
    if data[:3] == b"ID3":
        i = 10 + ((data[6] & 127) << 21 | (data[7] & 127) << 14 | (data[8] & 127) << 7 | (data[9] & 127))
        if data[5] & 0x10:
            i += 10                  # ID3v2.4 footer
    frames: list[Frame] = []
    while (f := _frame(data, i)) is not None and i + f.length <= len(data):
        frames.append(f)
        i += f.length
    if not frames:
        raise Mp3Error("no mp3 frames")
    tail = data[i:]
    if tail and not (tail[:3] == b"TAG" or tail[:8] == b"APETAGEX" or len(tail) < frames[-1].length):
        raise Mp3Error(f"{len(tail)} unexpected bytes after the last frame")
    first = frames[0]
    shape = (first.mpeg1, first.sample_rate, first.mono)
    if any((f.mpeg1, f.sample_rate, f.mono) != shape for f in frames):
        raise Mp3Error("the format changes mid-file")
    if any(f.crc for f in frames):
        raise Mp3Error("CRC-protected frames")
    x = first.side_offset + first.side_length
    info = first if data[x:x + 4] in (b"Xing", b"Info") else None
    audio = frames[1:] if info else frames
    if not audio:
        raise Mp3Error("no audio frames")
    return Mp3(data, audio, info, tail)


def _granule_gain_bits(f: Frame) -> list[int]:
    """Bit offsets (in the file) of each granule/channel's global_gain."""
    nch = 1 if f.mono else 2
    if f.mpeg1:
        start, granules, size = 9 + (5 if nch == 1 else 3) + 4 * nch, 2, 59
    else:
        start, granules, size = 8 + (1 if nch == 1 else 2), 1, 63
    base = f.side_offset * 8 + start + 21           # part2_3_length (12) + big_values (9) come first
    return [base + k * size for k in range(granules * nch)]


def _get8(b: bytearray, bit: int) -> int:
    byte, shift = bit >> 3, bit & 7
    return ((b[byte] << 8 | b[byte + 1]) >> (8 - shift)) & 0xFF


def _set8(b: bytearray, bit: int, value: int) -> None:
    byte, shift = bit >> 3, bit & 7
    word = (b[byte] << 8 | b[byte + 1]) & ~(0xFF << (8 - shift)) | (value << (8 - shift))
    b[byte], b[byte + 1] = (word >> 8) & 0xFF, word & 0xFF


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/ARC, as used by the LAME tag."""
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def edit(data: bytes, head: int = 0, tail: int = 0, steps: int = 0) -> bytes:
    """Drop `head`/`tail` audio frames and change the volume by `steps` global_gain steps (~1.5 dB each)."""
    mp3 = parse(data)
    n = len(mp3.frames)
    head, tail = max(0, head), max(0, tail)
    if head + tail >= n:
        head = tail = 0                               # never trim a clip away; keep it whole
    kept = mp3.frames[head:n - tail]
    out = bytearray(data[:(mp3.info or mp3.frames[0]).offset])     # ID3v2 tag, unchanged
    info_at = len(out)
    if mp3.info is not None:
        out += data[mp3.info.offset:mp3.info.offset + mp3.info.length]
    audio_at = len(out)
    for f in kept:
        frame = bytearray(data[f.offset:f.offset + f.length])
        if steps:
            for bit in _granule_gain_bits(f):
                local = bit - f.offset * 8
                _set8(frame, local, max(0, min(255, _get8(frame, local) + steps)))
        out += frame
    audio_end = len(out)
    out += mp3.tail
    if mp3.info is not None:
        _update_info(mp3, out, info_at, audio_at, audio_end, len(kept), trimmed=bool(head or tail),
                     trimmed_tail=tail > 0)
    return bytes(out)


def _update_info(mp3: Mp3, out: bytearray, info_at: int, audio_at: int, audio_end: int, frames: int,
                 trimmed: bool, trimmed_tail: bool) -> None:
    info = mp3.info
    assert info is not None and info_at == info.offset          # the prefix before it is copied unchanged
    x = info.side_offset + info.side_length
    flags = int.from_bytes(out[x + 4:x + 8], "big")
    p = x + 8
    if flags & 1:
        out[p:p + 4] = frames.to_bytes(4, "big")
        p += 4
    if flags & 2:
        out[p:p + 4] = (audio_end - info_at).to_bytes(4, "big")
        p += 4
    if flags & 4 and trimmed:
        out[p:p + 100] = bytes(i * 256 // 100 for i in range(100))    # linear seek table
        p += 100
    lame = mp3.lame
    if lame is None:
        return
    o = lame
    if trimmed_tail:                                  # the end padding was in the dropped frames
        out[o + 22] &= 0xF0
        out[o + 23] = 0
    out[o + 28:o + 32] = (audio_end - info_at).to_bytes(4, "big")
    out[o + 32:o + 34] = crc16(out[audio_at:audio_end]).to_bytes(2, "big")
    # The tag CRC covers the Info frame's first 190 bytes (zero-padded), with the CRC field itself as zero.
    out[o + 34:o + 36] = b"\0\0"
    head = bytes(out[info_at:info_at + min(190, info.length)]).ljust(190, b"\0")
    out[o + 34:o + 36] = crc16(head).to_bytes(2, "big")


@dataclass(frozen=True)
class Timing:
    frames: int              # audio frames
    samples_per_frame: int
    sample_rate: int
    skip: int                # samples a decoder skips at the start (encoder delay + decoder delay)
    seconds: float           # decoded length


def timing(data: bytes) -> Timing:
    mp3 = parse(data)
    f, n = mp3.first, len(mp3.frames)
    if mp3.lame is not None:
        delay, padding = mp3.delay_padding()
        skip, length = delay + DECODER_DELAY, n * f.samples - delay - padding
    else:
        skip, length = 0, n * f.samples
    return Timing(n, f.samples, f.sample_rate, skip, max(0, length) / f.sample_rate)


def frames_for_trim(t: Timing, sound_start: float, sound_end: float | None, margin: int = 1) -> tuple[int, int]:
    """How many whole audio frames lie before `sound_start` and after `sound_end` (seconds of decoded audio; None =
    no trailing silence), keeping `margin` extra frames on each side. The margin also covers the bit reservoir: a
    frame can borrow bits from the frame before it."""
    spf = t.samples_per_frame
    head = int((sound_start * t.sample_rate + t.skip) // spf) - margin
    if sound_end is None:
        tail = 0
    else:
        last = math.ceil((sound_end * t.sample_rate + t.skip) / spf)      # frames before this one hold the sound
        tail = t.frames - last - margin
    head, tail = max(0, head), max(0, tail)
    return (head, tail) if head + tail < t.frames else (0, 0)
