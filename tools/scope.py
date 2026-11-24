#!/usr/bin/env python3
"""scope.py : terminal spectrum waterfall from the radio's own receiver, over the debug channel.

    scope.py                          full UHF lock range (393-572 MHz), one sample per column
    scope.py 430 450 --step 0.025     every 25 kHz channel, each column = max of its channels
    scope.py --once                   one sweep, then exit
    options: --avg 0..3   RSSI averaging (AT1846S 0x5A[11:10]); 3 = stock (default, accurate),
                          2/1/0 = faster but read high and miss weak/strong contrast (coarse look only)
             --dwell MS   wait after each retune before reading RSSI (default per --avg)
             --floor/--ceil DB   fixed colour scale (default: auto from the data)
             --csv FILE   append every sweep (unix time, then one RSSI per sampled frequency)
             --no-verify  skip the per-step synthesizer readback check (a bit faster, may log bad steps)

Needs the debug firmware (patches/mkpatch_debug.py baseline or a mod image), radio on the standby
screen, not transmitting. RX only. Each step drives the AT1846S directly: write the frequency word
(0x29/0x2A = f_kHz x 16), restart RX (0x30 bit 5 off/on: the synthesizer only takes a new frequency on
RX start, and locks in < 2 ms), wait for RSSI to settle, read 0x1B[15:8] (dB). The restart resets the
RSSI/AGC path, so the dwell sets the speed: ~30 ms/step (full UHF, 1 sample per column: a few s/row;
every 25 kHz channel: ~3.5 min/row). Faster averaging (--avg 1/0, ~16 ms/step) reads 6-16 dB high.
Each step is verified against the synthesizer readback (VCO/6 in 1/4424.6 kHz units), because a write
occasionally collides with the firmware's own RF code on the shared bus.
While scanning, dual watch and power save are paused (power save sleeps the front ends, which shows
as a uniform raised floor), and the RX front end for the scanned band is switched on (chip reg 0x1F,
at1846_reg1f_shadow; stock only enables the band of the active VFO). On exit (also Ctrl-C) the
averaging, power save, dual watch and the VFO are restored through the stock retune path, and the
chip is reset (OVERVIEW recipe) if it wedged.
Frequencies outside the measured lock ranges (129.84-178.17 / 194.75-288.73 / 389.48-577.6 MHz)
show as blank columns. The radio's squelch still runs, so it may unmute on strong carriers.
"""
import argparse
import os
import shutil
import struct
import sys
import time

from radio import Radio

# firmware (D878UV V4.00) addresses: annotations/names.txt, globals.txt
AT_READ, AT_WRITE = 0x0807A73A, 0x0807A6FC
AT_RESET, AT_INIT = 0x0805E870, 0x0805E8CE
SIDES_RELOAD, CHAN_APPLY, RF_CMD_POST, APP_EVENT_POST = 0x080630FE, 0x08016112, 0x08061772, 0x08042828
RF_TX_ACTIVE, SCREEN_ID, RADIO_SETTINGS = 0x200001B1, 0x20000951, 0x20007FF8
DUAL_WATCH = RADIO_SETTINGS + 0x2D
POWER_SAVE = RADIO_SETTINGS + 0x0B
FRONTEND_SHADOW = 0x20000C0C   # at1846_reg1f_shadow: chip GPIOs, [9:8] VHF, [1:0] UHF front end, 11 = on

LOCK_RANGES = ((129.84, 178.17, 6), (194.75, 288.73, 4), (389.48, 577.6, 2))   # MHz, divider
DWELL = {3: 25.0, 2: 20.0, 1: 15.0, 0: 15.0}      # ms
RB_PER_KHZ = 4424.6                                # readback units per kHz of VCO/6
PALETTE = [16, 17, 18, 19, 20, 21, 27, 33, 39, 45, 51, 50, 49, 48, 47, 46, 82, 118, 154, 190, 226,
           220, 214, 208, 202, 196, 197, 198, 199, 200, 201, 207, 213, 219, 225, 231]


def divider(f):
    for lo, hi, d in LOCK_RANGES:
        if lo <= f <= hi:
            return d
    return None


class Scope:
    def __init__(self, r, avg, dwell, verify):
        self.r, self.verify = r, verify
        self.dwell = dwell / 1e3
        self.bad = 0
        if r.peek(RF_TX_ACTIVE, 1)[0]:
            sys.exit("radio is transmitting")
        if r.peek(SCREEN_ID, 1)[0] != 0:
            sys.exit("radio is not on the standby screen")
        self.dw = r.peek(DUAL_WATCH, 1)[0]
        self.ps = r.peek(POWER_SAVE, 1)[0]
        r.poke(DUAL_WATCH, b"\x00")
        r.poke(POWER_SAVE, b"\x00")             # power save sleeps the front ends: uniform raised floor
        time.sleep(0.4)                         # let a pending dual-watch side switch finish
        self.v30 = self.rd(0x30)
        self.v5a = self.rd(0x5A)
        self.avg = avg
        self.band = None
        self.setup()

    def setup(self):
        self.wr(0x5A, (self.v5a & ~0x0C00) | (self.avg << 10))
        self.wr(0x05, 0x8763)                    # normal reference mode (readback scale assumes it)
        self.band = None

    def frontend(self, f):
        """Enable the RX front end for f (stock: at1846_vhf_frontend_set / at1846_uhf_frontend_set), disable the other."""
        band = "uhf" if f >= 300 else "vhf"
        if band == self.band:
            return
        v = struct.unpack("<H", self.r.peek(FRONTEND_SHADOW, 2))[0] & 0xFCFC
        v |= 0x0203 if band == "uhf" else 0x0302
        self.r.poke(FRONTEND_SHADOW, struct.pack("<H", v))
        self.wr(0x1F, v)
        self.band = band

    def call(self, fn, *a):
        return self.r.call(fn | 1, *a)[0]

    def rd(self, reg):
        for _ in range(4):
            v = self.call(AT_READ, reg) & 0xFFFF
            if v != 0xFFFF:                      # bus collision with the RF task
                return v
        return None

    def wr(self, reg, v):
        self.call(AT_WRITE, reg, v)

    def step(self, f):
        """Retune to f MHz, return RSSI dB or None (unreachable / failed)."""
        d = divider(f)
        if d is None:
            return None
        self.frontend(f)
        for attempt in range(2):
            w = int(round(f * 16000))
            self.wr(0x29, w >> 16)
            self.wr(0x2A, w & 0xFFFF)
            self.wr(0x30, self.v30 & ~0x20)
            self.wr(0x30, self.v30)
            t0 = time.perf_counter()
            ok = True
            if self.verify:
                hi, lo = self.rd(0x29), self.rd(0x2A)
                ok = hi is not None and lo is not None and \
                    abs(((hi << 16) | lo) / RB_PER_KHZ * 6 / d - f * 1000) < 50
            while time.perf_counter() - t0 < self.dwell:
                pass
            if ok:
                v = sorted(x >> 8 for x in (self.rd(0x1B), self.rd(0x1B), self.rd(0x1B)) if x is not None)
                if v:                            # median: drops the odd bus-glitch read (0xE1, 0xFF..)
                    self.bad = 0
                    return float(v[len(v) // 2])
        self.bad += 1
        if self.bad >= 3:                        # synth wedged? (OVERVIEW: chip reset recipe)
            self.reset_chip()
        return None

    def reset_chip(self):
        self.call(AT_RESET)
        time.sleep(0.3)
        self.call(AT_INIT)
        time.sleep(0.5)
        self.v30 = self.rd(0x30) or self.v30
        self.setup()
        self.bad = 0

    def restore(self):
        r = self.r
        try:
            self.wr(0x5A, self.v5a)
        finally:
            r.poke(DUAL_WATCH, bytes([self.dw]))
            r.poke(POWER_SAVE, bytes([self.ps]))
            for attempt in range(2):
                for fn in (SIDES_RELOAD, CHAN_APPLY):
                    self.call(fn)
                self.call(RF_CMD_POST, 1)
                time.sleep(0.4)
                self.call(APP_EVENT_POST, 0x7FFFFFFF)
                locked = sum((self.rd(0x0D) or 0) >> 15 for _ in range(5)) >= 4
                if locked:
                    return True
                self.call(AT_RESET)
                time.sleep(0.3)
                self.call(AT_INIT)
                time.sleep(0.5)
            print("warning: radio did not re-lock after the scan; power-cycle it", file=sys.stderr)
            return False


def color(v, lo, hi):
    if v is None:
        return "\x1b[0m "
    x = (v - lo) / max(hi - lo, 1)
    i = min(len(PALETTE) - 1, max(0, int(x * len(PALETTE))))
    return "\x1b[48;5;%dm " % PALETTE[i]


def axis(cols, start, span, label_w):
    line = [" "] * cols
    marks = []
    every = max(10, cols // 8)
    for c in range(0, cols - 7, every):
        txt = "%.1f" % (start + span * (c + 0.5) / cols)
        line[c:c + len(txt)] = txt
        marks.append(c)
    tick = "".join("|" if c in marks else "-" for c in range(cols))
    return " " * label_w + "".join(line) + "\n" + " " * label_w + tick


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", nargs="?", type=float, default=393.0, help="MHz (default 393)")
    ap.add_argument("stop", nargs="?", type=float, default=572.0, help="MHz (default 572)")
    ap.add_argument("--step", type=float, help="MHz between samples (default: one per column)")
    ap.add_argument("--avg", type=int, choices=(0, 1, 2, 3), default=3)
    ap.add_argument("--dwell", type=float, help="ms after retune before reading RSSI")
    ap.add_argument("--floor", type=float)
    ap.add_argument("--ceil", type=float)
    ap.add_argument("--cols", type=int, help="columns (default: terminal width)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--csv")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--port")
    a = ap.parse_args()
    if a.stop <= a.start:
        sys.exit("stop must be above start")

    label_w = 9
    cols = a.cols or max(20, shutil.get_terminal_size((120, 40)).columns - label_w - 12)
    span = a.stop - a.start
    if a.step:
        freqs = [a.start + i * a.step for i in range(int(span / a.step) + 1)]
    else:
        freqs = [a.start + span * (c + 0.5) / cols for c in range(cols)]
    col_of = [min(cols - 1, int((f - a.start) / span * cols)) for f in freqs]

    r = Radio(a.port) if a.port else Radio()
    s = Scope(r, a.avg, a.dwell if a.dwell is not None else DWELL[a.avg], not a.no_verify)
    csv = open(a.csv, "a") if a.csv else None
    seen = []
    try:
        print(axis(cols, a.start, span, label_w))
        row = 0
        while True:
            t0 = time.time()
            col = [None] * cols
            vals = []
            s.band = None
            for f, c in zip(freqs, col_of):
                if r.peek(RF_TX_ACTIVE, 1)[0]:
                    raise SystemExit("radio started transmitting: stopping")
                v = s.step(f)
                vals.append(v)
                if v is not None and (col[c] is None or v > col[c]):
                    col[c] = v
            dt = time.time() - t0
            good = [v for v in col if v is not None]
            seen = (seen + good)[-cols * 30:]
            srt = sorted(seen) or [0]
            lo = a.floor if a.floor is not None else srt[len(srt) // 20]
            hi = a.ceil if a.ceil is not None else max(srt[-1], lo + 15)
            pk = max(range(cols), key=lambda c: col[c] if col[c] is not None else -1)
            pkf = a.start + span * (pk + 0.5) / cols
            print("%s " % time.strftime("%H:%M:%S") + "".join(color(v, lo, hi) for v in col) +
                  "\x1b[0m %5.1f %3.0f" % (pkf, col[pk] or 0))
            if csv:
                csv.write("%.1f," % t0 + ",".join("" if v is None else "%.1f" % v for v in vals) + "\n")
                csv.flush()
            row += 1
            if row == 1:
                print("%sone sweep: %d steps in %.1f s (%.1f ms/step); colour %d-%d dB; right: peak MHz, dB"
                      % (" " * label_w, len(freqs), dt, dt / len(freqs) * 1e3, lo, hi), file=sys.stderr)
            if a.once:
                break
            if row % 25 == 0:
                print(axis(cols, a.start, span, label_w))
    except KeyboardInterrupt:
        pass
    finally:
        print("\x1b[0m", end="")
        if csv:
            csv.close()
        s.restore()


if __name__ == "__main__":
    main()
