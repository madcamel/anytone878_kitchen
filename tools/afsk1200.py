#!/usr/bin/env python3
"""afsk1200.py : reference Bell-202 AFSK1200 / AX.25 decoder (and modulator) for APRS captures.

  afsk1200.py FILE.wav...      decode, print one TNC2 line per good frame (SRC>DST,PATH:info)
  afsk1200.py --selftest       modulate known frames (clean, noisy, de-emphasis tilt, 7200-11025 Hz)
                               and require they decode

Pure Python, integer-friendly on purpose: it is the model for the MCU demodulator (TIM6 ISR on
PC0 audio, docs/aprs.md), so every stage maps to something cheap in Thumb-2:
  DC block (IIR) -> 1200/2200 Hz quadrature correlators over one bit (fs/1200 samples)
  -> mark-space energy difference -> DPLL bit clock -> NRZI -> HDLC (flags, unstuffing) -> FCS.
Several slicers with different mark/space weights run in parallel (FM de-emphasis tilts the tones;
the radio's audio path tilt is unknown), frames are de-duplicated. 8-bit unsigned or 16-bit WAV.
"""
import math
import random
import struct
import sys
import wave

MARK, SPACE, BAUD = 1200, 2200, 1200
GAINS = (1.0, 0.5, 0.7, 1.4, 2.0)   # space-energy weights tried in parallel


def crc16_x25(data):
    """CRC-16/X.25 = AX.25 FCS (reflected 0x1021, init/xorout 0xFFFF); firmware crc16_x25."""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c ^ 0xFFFF


# ---------- AX.25 helpers ----------
def _addr(call, last, cbit=False):
    if call.endswith("*"):                  # digipeated (H bit)
        call, cbit = call[:-1], True
    c, _, ssid = call.partition("-")
    c = (c.upper() + "      ")[:6]
    return bytes(ord(x) << 1 for x in c) + bytes([0x60 | (int(ssid or 0) << 1) | (0x80 if cbit else 0) | int(last)])


def ax25_ui(src, dst, path, info):
    """Frame without flags, with FCS (low byte first)."""
    calls = [dst, src] + list(path)
    a = b"".join(_addr(c, i == len(calls) - 1) for i, c in enumerate(calls))
    body = a + b"\x03\xf0" + info
    return body + struct.pack("<H", crc16_x25(body))


def tnc2(frame):
    """frame = body without FCS."""
    def call(b):
        s = "".join(chr(x >> 1) for x in b[:6]).strip()
        ssid = (b[6] >> 1) & 15
        return s + ("-%d" % ssid if ssid else "")
    addrs, i = [], 0
    while i + 7 <= len(frame):
        addrs.append((call(frame[i:i + 7]), bool(frame[i + 6] & 0x80)))
        last = frame[i + 6] & 1
        i += 7
        if last:
            break
    if len(addrs) < 2:
        return None
    path = ",".join(c + ("*" if h else "") for c, h in addrs[2:])
    info = frame[i + 2:] if frame[i:i + 2] == b"\x03\xf0" else frame[i:]
    return "%s>%s%s:%s" % (addrs[1][0], addrs[0][0], "," + path if path else "",
                           info.decode("latin-1").replace("\r", "\\r").replace("\n", "\\n"))


# ---------- modulator (self-test / test vectors) ----------
def hdlc_bits(frame, preamble_flags=40, tail_flags=4):
    bits = []
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits += flag * preamble_flags
    ones = 0
    for b in frame:
        for k in range(8):
            bit = (b >> k) & 1
            bits.append(bit)
            ones = ones + 1 if bit else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    bits += flag * tail_flags
    return bits


def modulate(frame, fs=9600, amp=0.4, noise=0.0, tilt=1.0, seed=1):
    """AFSK samples in [-1,1]. tilt scales the 2200 Hz amplitude (de-emphasis ~0.55)."""
    rnd = random.Random(seed)
    out, ph, tone, t = [], 0.0, 1, 0.0
    spb = fs / BAUD
    acc = 0.0
    for bit in hdlc_bits(frame):
        if bit == 0:
            tone ^= 1                       # NRZI: 0 = change
        f, a = (MARK, amp) if tone else (SPACE, amp * tilt)
        acc += spb
        n = int(acc)
        acc -= n
        for _ in range(n):
            ph += 2 * math.pi * f / fs
            out.append(a * math.sin(ph) + noise * rnd.gauss(0, 1))
    return out


# ---------- demodulator ----------
class Slicer:
    """Correlator demod + DPLL + NRZI + HDLC for one space gain."""

    def __init__(self, fs, gain, sink):
        self.n = max(4, round(fs / BAUD))
        self.tm = [(math.cos(2 * math.pi * MARK * k / fs), math.sin(2 * math.pi * MARK * k / fs)) for k in range(self.n)]
        self.ts = [(math.cos(2 * math.pi * SPACE * k / fs), math.sin(2 * math.pi * SPACE * k / fs)) for k in range(self.n)]
        self.win = [0.0] * self.n
        self.wi = 0
        self.gain = gain
        self.step = int(2 ** 32 * BAUD / fs)
        self.phase = 0
        self.last_d = 0
        self.last_bit = 0
        self.sink = sink
        self.reg = 0
        self.ones = 0
        self.buf = None
        self.byte = 0
        self.nbits = 0

    def sample(self, x):
        self.win[self.wi] = x
        self.wi = (self.wi + 1) % self.n
        mc = ms = sc = ss = 0.0
        for k in range(self.n):
            v = self.win[(self.wi + k) % self.n]
            mc += v * self.tm[k][0]; ms += v * self.tm[k][1]
            sc += v * self.ts[k][0]; ss += v * self.ts[k][1]
        d = 1 if (mc * mc + ms * ms) > self.gain * (sc * sc + ss * ss) else 0
        if d != self.last_d:                # transition: pull bit clock toward mid-bit
            self.last_d = d
            ph = self.phase - (1 << 32) if self.phase >= 1 << 31 else self.phase
            self.phase = int(ph * 0.6) & 0xFFFFFFFF
        old = self.phase
        self.phase = (self.phase + self.step) & 0xFFFFFFFF
        if old < (1 << 31) <= self.phase:   # crossed mid-bit: sample
            self.bit(1 if d == self.last_bit else 0)
            self.last_bit = d

    def bit(self, b):
        self.reg = ((self.reg << 1) | b) & 0xFF
        if self.reg == 0x7E:                # flag (bit order irrelevant: palindrome)
            if self.buf is not None and len(self.buf) >= 18 and self.nbits == 7:
                self.done(bytes(self.buf))
            self.buf, self.byte, self.nbits, self.ones = bytearray(), 0, 0, 0
            return
        if self.buf is None:
            return
        if b:
            self.ones += 1
            if self.ones > 6:               # abort / idle
                self.buf = None
                return
        else:
            if self.ones == 5:              # stuffed zero
                self.ones = 0
                return
            self.ones = 0
        self.byte |= b << self.nbits
        self.nbits += 1
        if self.nbits == 8:
            self.buf.append(self.byte)
            self.byte, self.nbits = 0, 0
            if len(self.buf) > 400:
                self.buf = None

    def done(self, raw):
        # the flag's first 7 bits were already shifted in as data: drop the partial byte
        if crc16_x25(raw[:-2]) == struct.unpack("<H", raw[-2:])[0]:
            self.sink(raw[:-2])


def demod(samples, fs):
    """samples: floats roughly in [-1,1]. Returns list of frames (body without FCS), de-duplicated, in order."""
    frames = []
    seen = set()

    def sink(f):
        if f not in seen:
            seen.add(f)
            frames.append(f)
    slicers = [Slicer(fs, g, sink) for g in GAINS]
    dc = 0.0
    for s in samples:
        dc += (s - dc) / 64.0               # DC block
        x = s - dc
        for sl in slicers:
            sl.sample(x)
    return frames


def read_wav(path):
    with wave.open(str(path)) as w:
        fs, n, width, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(n)
    if width == 1:
        s = [(b - 128) / 128.0 for b in raw[::ch]]
    elif width == 2:
        s = [v / 32768.0 for v in struct.unpack("<%dh" % (len(raw) // 2), raw)[::ch]]
    else:
        raise ValueError("%s: %d-byte samples unsupported" % (path, width))
    return s, fs


def write_wav8(path, data, fs):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(int(fs))
        w.writeframes(bytes(data))


def selftest():
    f1 = ax25_ui("N0CALL-9", "APRS", ["WIDE1-1", "WIDE2-1"], b"!4903.50N/07201.75W-Test 123 ~ tilde")
    f2 = ax25_ui("TEST-1", "APDR16", [], b":N0CALL   :hello{01")
    cases = [(9600, 0.0, 1.0), (9600, 0.15, 1.0), (9600, 0.1, 0.5), (9600, 0.05, 2.0),
             (9024, 0.1, 0.55), (7200, 0.05, 1.0), (11025, 0.1, 0.6)]
    for fs, noise, tilt in cases:
        s = [0.0] * 500 + modulate(f1, fs, noise=noise, tilt=tilt) + [0.0] * 300 + \
            modulate(f2, fs, noise=noise, tilt=tilt, seed=2) + [0.0] * 300
        got = demod(s, fs)
        want = [f1[:-2], f2[:-2]]
        assert got == want, "fs=%d noise=%.2f tilt=%.2f: got %s" % (fs, noise, tilt, [tnc2(g) for g in got])
    assert tnc2(f1[:-2]) == "N0CALL-9>APRS,WIDE1-1,WIDE2-1:!4903.50N/07201.75W-Test 123 ~ tilde"
    # 8-bit quantised, the format the radio capture produces
    s = modulate(f1, 9600, amp=0.3, noise=0.05, tilt=0.6)
    q = [min(255, max(0, int(128 + 127 * v))) for v in s]
    assert demod([(b - 128) / 128.0 for b in q], 9600) == [f1[:-2]]
    print("afsk1200 selftest OK (%d rate/noise/tilt cases + 8-bit)" % len(cases))


def main():
    args = sys.argv[1:]
    if args == ["--selftest"]:
        selftest()
        return
    if not args:
        sys.exit(__doc__)
    bad = 0
    for p in args:
        try:
            s, fs = read_wav(p)
        except (OSError, ValueError, wave.Error) as e:
            print("%s: %s" % (p, e)); bad += 1
            continue
        fr = demod(s, fs)
        for f in fr:
            print("%s: %s" % (p, tnc2(f)))
        if not fr:
            print("%s: no frame" % p)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
