#!/usr/bin/env python3
"""aprsrx.py [--watch] : status of the aprs_rx mod on a running radio (debug channel).

Shows whether the demodulator runs (TIM6 mode 5), whether the current channel has APRS Receive on,
the mod's frame/drop counters, and the last frame handed to the stock parser (TNC2). --watch polls
every second and prints each new frame. Holds the radio port while running (not with aprscapctl).
"""
import struct
import sys
import time

import afsk1200
from radio import Radio

RX_FREQ, CHAN_REC, MODE, SQ = 0x200003C0, 0x200003E4, 0x20000178, 0x2000003E
FRAME, FRAME_LEN = 0x2000C10F, 0x20000F73
OFF_TICKS, OFF_FRAMES, OFF_DROPPED = 0, 36, 40   # patches/mods/aprs_rx/aprs_rx.c struct state


def snapshot(r, m):
    rec = struct.unpack("<I", r.peek(CHAN_REC, 4))[0]
    st = r.peek(m["ram"], 44)
    return dict(freq=struct.unpack("<I", r.peek(RX_FREQ, 4))[0] * 10, mode=r.peek(MODE, 1)[0],
                aprs_on=bool(r.peek(rec + 0x21, 1)[0] & 0x20), sq=r.peek(SQ, 1)[0],
                ticks=struct.unpack_from("<I", st, OFF_TICKS)[0],
                frames=struct.unpack_from("<I", st, OFF_FRAMES)[0],
                dropped=struct.unpack_from("<I", st, OFF_DROPPED)[0],
                init=r.peek(m["ram"] + m["ram_len"] - 4, 4) == b"aRx1")


def last_frame(r):
    n = r.peek(FRAME_LEN, 1)[0]
    raw = r.peek(FRAME, 7 + n)
    if raw[:7] != b"+APRS:~" or n < 20:
        return None
    return afsk1200.tnc2(raw[7:7 + n - 6])     # body = n - 6 bytes


def main():
    watch = sys.argv[1:] == ["--watch"]
    r = Radio()
    m = r.mods().get("aprs_rx")
    if not m:
        sys.exit("radio has no aprs_rx mod")
    s = snapshot(r, m)
    t0, tk0 = time.monotonic(), s["ticks"]
    print("%.4f MHz  APRS Receive %s  demod %s  squelch %s  frames %d  dropped %d%s" % (
        s["freq"] / 1e6, "on" if s["aprs_on"] else "OFF", "running (mode 5)" if s["mode"] == 5 else "idle (mode %d)" % s["mode"],
        "open" if s["sq"] else "closed", s["frames"], s["dropped"], "" if s["init"] else "  (state not initialised yet)"))
    lf = last_frame(r)
    if lf:
        print("last frame handed to the parser:", lf)
    if not watch:
        time.sleep(1)
        s2 = snapshot(r, m)
        if s2["mode"] == 5:
            print("sample rate %.0f Hz" % ((s2["ticks"] - tk0) / (time.monotonic() - t0)))
        return
    frames = s["frames"]
    while True:
        time.sleep(1)
        s = snapshot(r, m)
        if s["frames"] != frames:
            print(time.strftime("%H:%M:%S"), "frame #%d:" % s["frames"], last_frame(r) or "(not handed over: channel/squelch conditions)")
            frames = s["frames"]


if __name__ == "__main__":
    main()
