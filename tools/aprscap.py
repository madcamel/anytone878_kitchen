#!/usr/bin/env python3
"""aprscap.py : record squelch-triggered RX audio bursts from the radio (capture firmware) as WAVs.

Needs an image with the `capture` mod (patches/modbuild.py capture); its RAM comes from the radio's
mod table, so it follows wherever the builder placed it. Run it through
scripts/aprscapctl start|stop|status; it holds the radio port, so other radio.py commands wait.

Loop (every POLL s): if tim6_audio_mode != 4 and the radio isn't transmitting and no channel tone
decoder owns TIM6 (modes 1-3), arm: zero the capture block, mode = 4, tim6_set_rate(period, 0),
TIM_Cmd(TIM6, 1). Retunes/TX reset the mode, so this re-arms after every one. When the device
freezes a capture (state 2) it reads the ring, unrolls it oldest-first, writes
out/aprscap/YYYYMMDD/cap_HHMMSS_<pid>_<n>.wav (+ .json: rx freq, trigger offset, truncation,
measured sample rate, decoded frames from tools/afsk1200.py), then re-arms.
On SIGTERM it stops TIM6 if it is still in mode 4 and exits.
"""
import datetime
import fcntl
import json
import os
import signal
import struct
import sys
import termios
import time

import serial

import afsk1200  # noqa: E402
import hcfg  # noqa: E402
from radio import Radio  # noqa: E402


class C:
    """Capture block layout (patches/mods/capture/capture.c); CAP/N filled from the mod table."""
    MODE = 0x20000178                   # tim6_audio_mode
    RATE = 9600
    PRE = 1920
    CAP = BUF = N = POST = None

    @classmethod
    def locate(cls, r):
        m = r.mods().get("capture")
        if not m:
            raise SystemExit("radio has no `capture` mod (flash out/D878UV_V4.00_base+capture/)")
        cls.CAP, cls.BUF, cls.N = m["ram"], m["ram"] + 16, m["ram_len"] - 16
        cls.POST = cls.N - cls.PRE

TIM6 = 0x40001000
TIM_CMD = 0x08006730
TIM6_SET_RATE = 0x0807B4F0
RF_TX_ACTIVE = 0x200001B1
RX_FREQ = 0x200003C0
CLK = 120_000_000                       # TIM6 clock; checked against the tick counter at runtime
PERIOD = CLK // C.RATE - 1               # 12499
POLL = 0.25
OUTDIR = hcfg.OUT / "aprscap"

stop = False


def on_term(*_):
    global stop
    stop = True


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def hdr(r):
    st, wr, left, trig, seq, ticks = struct.unpack("<BxHHHII", r.peek(C.CAP, 16))
    return dict(state=st, wr=wr, left=left, trig=trig, seq=seq, ticks=ticks)


def arm(r):
    r.poke(C.CAP, bytes(16))
    r.poke(C.MODE, b"\x04")
    r.call(TIM6_SET_RATE, PERIOD, 0)
    r.call(TIM_CMD, TIM6, 1)


def save(r, h, rate, n):
    buf = r.peek(C.BUF, C.N)
    r.poke(C.CAP, b"\x00")              # re-arm right away; the ring keeps recording
    wr = h["wr"]
    data = buf[wr:] + buf[:wr]
    pre = (h["trig"] - wr) % C.N
    post = C.N - pre
    now = datetime.datetime.now()
    d = OUTDIR / now.strftime("%Y%m%d")
    d.mkdir(parents=True, exist_ok=True)
    stem = d / ("cap_%s_%d_%d" % (now.strftime("%H%M%S"), os.getpid(), n))
    fs = round(rate) if rate else C.RATE
    afsk1200.write_wav8(stem.with_suffix(".wav"), data, fs)
    rx = struct.unpack("<I", r.peek(RX_FREQ, 4))[0] * 10
    frames = [afsk1200.tnc2(f) for f in afsk1200.demod([(b - 128) / 128.0 for b in data], fs)]
    meta = dict(time=now.isoformat(timespec="seconds"), rx_hz=rx, fs=fs, fs_nominal=C.RATE,
                pre_samples=pre, post_samples=post, truncated=post >= C.POST, frames=frames,
                lo=min(data), hi=max(data))
    stem.with_suffix(".json").write_text(json.dumps(meta, indent=1) + "\n")
    log("capture %s  %.3f MHz  level %d..%d  %s%s" % (stem.name, rx / 1e6, meta["lo"], meta["hi"],
        "TRUNCATED " if meta["truncated"] else "", "; ".join(frames) if frames else "no frame decoded"))


def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    lk = open(OUTDIR / "aprscap.lock", "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("aprscap already running")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    r = Radio()
    C.locate(r)
    log("started pid %d, period %d (%d Hz nominal), ring %d samples, pre %d, post %d"
        % (os.getpid(), PERIOD, C.RATE, C.N, C.PRE, C.POST))
    n = 0
    warned = None
    arms = []                           # recent arm times: frequent re-arms = radio keeps retuning
    last_freq = None
    rate = None
    t0 = tk0 = None
    last_report = time.monotonic()
    try:
        while not stop:
            try:
                mode = r.peek(C.MODE, 1)[0]
                if mode != 4:
                    t0 = None
                    tx = r.peek(RF_TX_ACTIVE, 1)[0]
                    why = "a channel tone decoder owns TIM6 (mode %d)" % mode if mode in (1, 2, 3) else \
                        "transmitting" if tx else None
                    if why:
                        if why != warned:
                            log("not arming:", why)
                            warned = why
                    else:
                        now = time.monotonic()
                        arms = [t for t in arms if now - t < 10]
                        if len(arms) >= 5:
                            if warned != "retune":
                                freqs = {struct.unpack("<I", r.peek(RX_FREQ, 4))[0] * 10 for _ in range(6)}
                                log("radio retunes constantly (RX freq seen: %s): dual watch (PF function"
                                    " 'Sub Ch Hide' toggles it) or scan is on. Turn it off; waiting."
                                    % ", ".join("%.4f" % (f / 1e6) for f in sorted(freqs)))
                                warned = "retune"
                            time.sleep(2)
                            continue
                        arm(r)
                        arms.append(now)
                        f = struct.unpack("<I", r.peek(RX_FREQ, 4))[0] * 10
                        if warned is None or f != last_freq or warned == "retune":
                            log("armed on %.4f MHz" % (f / 1e6))
                        last_freq = f
                        warned = ""
                    time.sleep(POLL)
                    continue
                h = hdr(r)
                now = time.monotonic()
                if t0 is None:
                    t0, tk0 = now, h["ticks"]
                elif now - t0 >= 5:
                    rate = (h["ticks"] - tk0) / (now - t0)
                    if now - last_report > 600:
                        log("alive: measured sample rate %.0f Hz, %d captures" % (rate, n))
                        last_report = now
                        if abs(rate - C.RATE) > C.RATE * 0.02:
                            log("WARNING: rate is %.0f, not %d: TIM6 clock isn't %d Hz" % (rate, C.RATE, CLK))
                if h["state"] == 2:
                    n += 1
                    save(r, h, rate, n)
                time.sleep(POLL)
            except (IOError, OSError, serial.SerialException, termios.error) as e:
                log("radio lost (%s); waiting for it to come back" % e)
                t0 = None
                last_err = None
                while not stop:
                    try:
                        r.reopen(wait=30)
                        C.locate(r)
                        log("radio back; ring %d samples at %#x" % (C.N, C.CAP))
                        break
                    except (IOError, OSError, serial.SerialException, termios.error, SystemExit) as e2:
                        time.sleep(5)
                        if str(e2) != last_err and "did not come back" not in str(e2):
                            log("still unavailable: %s" % e2)
                            last_err = str(e2)
    finally:
        try:
            if r.peek(C.MODE, 1)[0] == 4:
                r.call(TIM_CMD, TIM6, 0)
                r.poke(C.MODE, b"\x00")
        except Exception as e:  # radio unplugged etc.
            log("cleanup failed:", e)
        log("stopped after %d captures" % n)


if __name__ == "__main__":
    main()
