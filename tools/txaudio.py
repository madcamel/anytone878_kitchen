#!/usr/bin/env python3
"""txaudio.py : transmit audio from the PC through the radio, over the USB data link (no audio cable).

    txaudio.py FILE.wav --yes                 key up, play the file, unkey
    txaudio.py --tts "N0CALL test" --yes      speak text (espeak / espeak-ng)
    txaudio.py FILE.wav --dry                 stream without keying (checks the chain, no RF)
    options: --level 0.1..1 (default 0.9)  --expect 147.105 (refuse unless TX is on this MHz)
             --max 60 (refuse longer clips, seconds)

How (docs/tx_audio.md): the radio's own data/beep DAC is a 76.8 kHz PWM on PB9 (TIM4 CH4), summed
into the TX audio. This tool keys PTT (key code 0x18), waits for rf_tx_active, then has DMA1 ch5,
paced by TIM1 at 8 kHz, copy samples from a RAM ring into TIM4's duty register while it streams the
audio into that ring with debug pokes (~16 KB/s of the ~100 KB/s link). Afterwards it restores
TIM1/TIM4/PB9/DMA and unkeys (also on errors or Ctrl-C).

Needs the debug channel v2 firmware (patches/mkpatch_debug.py; any patches/modbuild.py image): v1
serves the link only every 10 ms and not at all while transmitting. Uses the radio's current
channel/VFO, power and CTCSS: whatever a PTT press would transmit with. Uses sox for conversion.
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

from radio import Radio

RATE = 8000
RING, N = 0x20016000, 2048                 # 2048 x u16 (256 ms) in free RAM above the mod RAM pool
TIM1, TIM4 = 0x40012C00, 0x40000800
TIM4_CCR4 = TIM4 + 0x40
DMA1_CH5 = 0x40020058                       # TIM1_UP request: CCR, CNDTR +4, CPAR +8, CMAR +0xC
RCC_APB2ENR, GPIOB_CRH = 0x40021018, 0x40010C04
GPIO_PB9_AF = 0x0807B39E                    # gpio_pb9_af_pwm_out (stock)
TIM4_PWM_START = 0x0806DFBA                 # afsk_pwm_tim4_ch4_start (stock)
RF_TX_ACTIVE, SCREEN_ID, TX_FREQ = 0x200001B1, 0x20000951, 0x200003B4
USB_VECTOR, DBG_FAST = 0x08004090, (0x080078EC, 0x08007B28)
CENTER, SWING = 500, 450                    # stock AFSK sine spans duty 0..1000 of a 1562 period
KEY_PTT_DOWN, KEY_PTT_UP = 0x18, 0x19


class Tx:
    def __init__(self, r):
        self.r = r
        self.saved = None

    def u32(self, a):
        return struct.unpack("<I", self.r.peek(a, 4))[0]

    def w32(self, a, v):
        self.r.poke(a, struct.pack("<I", v & 0xFFFFFFFF), any_addr=True)

    def put(self, idx, vals):
        i = 0
        while i < len(vals):
            k = min(28, len(vals) - i, N - idx)
            self.r.poke(RING + 2 * idx, struct.pack("<%dH" % k, *vals[i:i + k]))
            i += k
            idx = (idx + k) % N

    def setup(self):
        u = self.u32
        self.saved = dict(apb2=u(RCC_APB2ENR), crh=u(GPIOB_CRH), t4cr1=u(TIM4), t4arr=u(TIM4 + 0x2C),
                          t4ccr4=u(TIM4_CCR4), t4dier=u(TIM4 + 0x0C))
        self.w32(RCC_APB2ENR, self.saved["apb2"] | (1 << 11))   # TIM1 clock (the firmware never uses TIM1)
        self.r.call(GPIO_PB9_AF)
        self.r.call(TIM4_PWM_START, 0x619, 0)                    # 120 MHz / 1562 = 76.8 kHz PWM on CH4
        self.w32(TIM4 + 0x0C, 0)                                  # the stock helper enables TIM4 update DMA
        self.w32(TIM4_CCR4, CENTER)

    def start(self):
        w = self.w32
        w(DMA1_CH5, 0)
        w(DMA1_CH5 + 8, TIM4_CCR4)
        w(DMA1_CH5 + 0xC, RING)
        w(DMA1_CH5 + 4, N)
        w(DMA1_CH5, 0x25B1)                 # EN, mem->periph, circular, MINC, 16/16-bit, priority high
        w(TIM1, 0)
        w(TIM1 + 0x28, 0)                   # PSC
        w(TIM1 + 0x2C, 120_000_000 // RATE - 1)
        w(TIM1 + 0x30, 0)                   # RCR
        w(TIM1 + 0x14, 1)                   # UG
        w(TIM1 + 0x10, 0)
        w(TIM1 + 0x0C, 0x100)               # update DMA request only: no TIM1 interrupt (IRQ 25 = debug v2)
        w(TIM1, 1)

    def stop(self):
        if not self.saved:
            return
        s, w = self.saved, self.w32
        w(TIM1, 0)
        w(TIM1 + 0x0C, 0)
        w(DMA1_CH5, 0)
        w(TIM4_CCR4, CENTER)
        w(TIM4, s["t4cr1"]); w(TIM4 + 0x0C, s["t4dier"]); w(TIM4 + 0x2C, s["t4arr"]); w(TIM4_CCR4, s["t4ccr4"])
        w(GPIOB_CRH, s["crh"])
        w(RCC_APB2ENR, s["apb2"])
        self.saved = None

    def stream(self, samples):
        data = samples + [CENTER] * (N + 800)
        self.put(0, data[:N])
        written, played, last, under = N, 0, 0, 0
        self.start()
        t0 = time.monotonic()
        while played < len(samples) + 400:
            pos = (N - self.u32(DMA1_CH5 + 4)) % N
            played += (pos - last) % N
            last = pos
            if played > written:
                under += 1
            room = N - 256 - (written - played)
            if room >= 28 and written < len(data):
                k = min(room, 280, len(data) - written)
                self.put(written % N, data[written:written + k])
                written += k
            if not self.u32(TIM4) & 1:      # something stopped TIM4 (e.g. a stock beep): restart it
                self.w32(TIM4, self.u32(TIM4) | 1)
        return played, time.monotonic() - t0, under


def convert(path, level):
    if not shutil.which("sox"):
        sys.exit("sox not found (apt install sox)")
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "a.wav")
        subprocess.run(["sox", path, "-r", str(RATE), "-c", "1", "-b", "16", out,
                        "gain", "-3", "highpass", "300", "lowpass", "3000", "norm", "-1", "pad", "0.3", "0.3"], check=True)
        with wave.open(out) as w:
            s = struct.unpack("<%dh" % w.getnframes(), w.readframes(w.getnframes()))
    a = SWING * level
    return [max(0, min(1000, int(CENTER + a * x / 32768))) for x in s]


def tts(text, d):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        sys.exit("no espeak / espeak-ng for --tts")
    path = os.path.join(d, "tts.wav")
    subprocess.run([exe, "-v", "en-us", "-s", "135", "-g", "6", text, "-w", path], check=True)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--tts")
    ap.add_argument("--dry", action="store_true", help="stream without keying the transmitter")
    ap.add_argument("--yes", action="store_true", help="confirm keying the transmitter")
    ap.add_argument("--level", type=float, default=0.9, help="0.1..1, fraction of the stock AFSK level")
    ap.add_argument("--expect", type=float, help="refuse unless the TX frequency is this (MHz)")
    ap.add_argument("--max", type=float, default=60, help="refuse clips longer than this (s)")
    a = ap.parse_args()
    if bool(a.file) == bool(a.tts):
        ap.error("give a FILE or --tts TEXT")
    if not a.dry and not a.yes:
        ap.error("this keys the transmitter: pass --yes (or --dry)")
    if not 0.1 <= a.level <= 1:
        ap.error("--level must be 0.1..1")
    with tempfile.TemporaryDirectory() as d:
        samples = convert(tts(a.tts, d) if a.tts else a.file, a.level)
    secs = len(samples) / RATE
    if secs > a.max:
        sys.exit("clip is %.1f s, longer than --max %.0f s" % (secs, a.max))

    r = Radio()
    tx = Tx(r)
    vec = tx.u32(USB_VECTOR)
    if not DBG_FAST[0] <= (vec & ~1) < DBG_FAST[1]:
        sys.exit("radio runs debug channel v1 (or none): flash a debug v2 image (patches/modbuild.py / mkpatch_debug.py)")
    if r.peek(SCREEN_ID, 1)[0] != 0 or r.peek(RF_TX_ACTIVE, 1)[0]:
        sys.exit("radio isn't idle on the standby screen")
    keyed = False
    try:
        if not a.dry:
            r.key(KEY_PTT_DOWN)
            keyed = True
            t0 = time.monotonic()
            while not r.peek(RF_TX_ACTIVE, 1)[0]:
                if time.monotonic() - t0 > 2:
                    raise RuntimeError("PTT didn't key the transmitter (TX inhibit / busy lock?)")
                time.sleep(0.02)
            mhz = tx.u32(TX_FREQ) / 1e5
            print("keyed on %.5f MHz" % mhz, flush=True)
            if a.expect is not None and abs(mhz - a.expect) > 0.0005:
                raise RuntimeError("TX is on %.5f MHz, not %.5f: unkeying" % (mhz, a.expect))
            time.sleep(0.3)                  # the stock key-up path reconfigures audio timers first
        tx.setup()
        played, dt, under = tx.stream(samples)
        print("%.2f s of audio in %.2f s (%.0f Hz), %d underruns%s" % (
            len(samples) / RATE, dt, played / dt, under,
            "" if a.dry else ", still keyed" if r.peek(RF_TX_ACTIVE, 1)[0] else ", TRANSMITTER DROPPED"))
    finally:
        tx.stop()
        if keyed:
            r.key(KEY_PTT_UP)
            t0 = time.monotonic()
            while r.peek(RF_TX_ACTIVE, 1)[0] and time.monotonic() - t0 < 2:
                time.sleep(0.02)
            print("unkeyed" if not r.peek(RF_TX_ACTIVE, 1)[0] else "WARNING: still transmitting (TOT will cut it)")


if __name__ == "__main__":
    main()
