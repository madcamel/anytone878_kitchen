#!/usr/bin/env python3
"""replay.py [IMAGE_DIR] [WAV...] : run the BUILT aprs_rx mod (unicorn) over recorded bursts and compare
with the reference decoder (tools/afsk1200.py). Default: out/D878UV_V4.00_base+aprs_rx and every
out/aprscap/*/cap_*.wav. Also reports the worst-case instruction count per TIM6 interrupt.

The capture WAVs are 8-bit samples of exactly what the demod reads (ADC >> 4 at 9600 Hz), so this is a
bit-exact rehearsal of the radio, minus timing."""
import glob
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT / "patches"), str(ROOT / "tools")]
import afsk1200 as A  # noqa: E402
import modbuild  # noqa: E402
import modemu  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402


def main():
    args = sys.argv[1:]
    d = Path(args.pop(0)) if args and Path(args[0]).is_dir() else ROOT / "out/D878UV_V4.00_base+aprs_rx"
    wavs = args or sorted(glob.glob(str(ROOT / "out/aprscap/*/cap_*.wav")))
    img = (d / (d.name + ".bin")).read_bytes()
    syms = {}
    for line in (d / "mods.map").read_text().splitlines():
        if line.startswith("#"):
            if " aprs_rx " in line + " ":
                ram = int(line.split("ram ")[1].split("+")[0], 16)
            continue
        a, n = line.split()
        syms[n] = int(a, 16)
    fw = {k: v[0] for k, v in modbuild.firmware_symbols().items()}
    tot_ref = tot_mod = only_ref = only_mod = 0
    worst = 0
    for w in wavs:
        s, fs = A.read_wav(w)
        if fs != 9600:
            print("%s: fs %d, skipped" % (w, fs)); continue
        ref = A.demod(s, fs)
        e = modemu.Emu(img)
        REC = 0x20010000
        e.w32(fw["rf_chan_rec"], REC); e.write(REC, bytes(0x40)); e.w8(REC + 0x21, 0x20)
        e.stub(fw["chan_is_digital"], lambda e: 0)
        e.stub(fw["sigdec_select_for_channel"], lambda e: e.w8(fw["tim6_audio_mode"], 0))
        e.stub(fw["tim6_set_rate"], lambda e: None)
        e.stub(fw["TIM_Cmd"], lambda e: None)
        got = []

        def parser(e):
            if e.r8(fw["aprs_rx_frame_ready"]):
                n = e.r8(fw["aprs_rx_frame_len"])
                got.append(e.read(fw["aprs_rx_frame"] + 7, n - 4))   # body + FCS (n = body + 6)
                e.w8(fw["aprs_rx_frame_ready"], 0)
        e.stub(fw["aprs_rx_frame_process"], parser)
        e.w8(fw["ana_rx_audio_open"], 1)
        e.call(syms["aprs_sigdec"])
        cnt = [0]
        e.u.hook_add(UC_HOOK_CODE, lambda u, a, sz, _: cnt.__setitem__(0, cnt[0] + 1),
                     begin=0x080E7C00, end=0x080F5800)
        for i, v in enumerate(s):
            e.w16(fw["adc_pc0_rx_audio"], min(255, max(0, int(round(v * 128 + 128)))) << 4)
            cnt[0] = 0
            e.run_from(0x08004A4C, regs=dict(r0=5), stack=(0x4444, 0x08000101))
            worst = max(worst, cnt[0])
            if i % 96 == 95:
                e.call(syms["aprs_p14"])
        e.call(syms["aprs_p14"])
        got = [g[:-2] for g in got]                # strip FCS -> body, like the reference
        r_set, g_set = set(ref), set(got)
        tot_ref += len(r_set); tot_mod += len(g_set)
        only_ref += len(r_set - g_set); only_mod += len(g_set - r_set)
        flag = "" if r_set == g_set else "   <-- differs: ref-only %d, mod-only %d" % (len(r_set - g_set), len(g_set - r_set))
        print("%s  ref %d  mod %d%s" % (Path(w).name, len(r_set), len(g_set), flag))
        for f in g_set - r_set:
            print("      mod-only:", A.tnc2(f))
        for f in r_set - g_set:
            print("      ref-only:", A.tnc2(f))
    print("TOTAL frames: reference %d, mod %d (ref-only %d, mod-only %d); worst-case %d mod instructions per interrupt"
          % (tot_ref, tot_mod, only_ref, only_mod, worst))


if __name__ == "__main__":
    main()
