"""aprs_rx: analog APRS receive on the MCU (AFSK1200 demod -> stock APRS stack, menus unhidden).
Either/or with capture (both take the TIM6 mode dispatch)."""
import struct
import sys
from pathlib import Path

NAME = "aprs_rx"
VERSION = 1
RAM = 1280
SOURCES = ["aprs_rx.c"]
HOOKS = [
    dict(site=0x08004A4C, orig="032801d106f092f8", asm="nop\nnop\nbl {aprs_tim6}",
         why="TIM6_IRQHandler: every mode except 1/2 reaches aprs_tim6(mode); 3 = stock zero-cross, 5 = demod"),
    dict(site=0x08060758, orig="adf798fb", asm="bl {aprs_sigdec}",
         why="rf_cmd_apply_lo -> sigdec_select_for_channel (1/2): start mode 5 on APRS channels"),
    dict(site=0x08060988, orig="adf780fa", asm="bl {aprs_sigdec}",
         why="rf_cmd_apply_lo -> sigdec_select_for_channel (2/2)"),
    dict(site=0x08029D64, orig="4ef05cfc", asm="bl {aprs_p14}",
         why="task_p14_housekeeping_loop -> aprs_rx_frame_process: hand over pending frames first"),
    dict(site=0x08078672, orig="fef793f9", asm="bl {aprs_fcs}",
         why="aprs_rx_frame_process -> ax25_fcs_check: our frames are CRC-checked already (0x7E-safe)"),
    dict(site=0x0807455C, orig="0020", asm="movs r0, #1",
         why="bt_board_probe: aprs_board_present starts as 1 (unhides Ana APRS Info / APRS Receive)"),
]
# state layout (aprs_rx.c struct state): frames counter for host statistics
OFF_FRAMES, OFF_DROPPED = 36, 40

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools"))
import afsk1200 as A  # noqa: E402


def test(e, m, fw):
    s = m["syms"]
    import math
    for name, f, fn in (("MC", 1200, math.cos), ("MS", 1200, math.sin), ("SC", 2200, math.cos), ("SS", 2200, math.sin)):
        want = bytes(round(127 * fn(2 * math.pi * f * k / 9600)) & 0xFF for k in range(8))
        assert e.read(s[name], 8) == want, "correlator table %s wrong" % name
    REC = 0x20010000                              # fake channel record
    e.w32(fw["rf_chan_rec"], REC)
    e.write(REC, bytes(0x40))
    e.w8(REC + 0x21, 0x20)                        # APRS Receive on
    digital = [0]
    rate, cmd = [], []
    e.stub(fw["chan_is_digital"], lambda e: digital[0])
    e.stub(fw["sigdec_select_for_channel"], lambda e: e.w8(fw["tim6_audio_mode"], 0))
    e.stub(fw["tim6_set_rate"], lambda e: rate.append((e.arg(0), e.arg(1))))
    e.stub(fw["TIM_Cmd"], lambda e: cmd.append((e.arg(0), e.arg(1))))
    zc = []
    e.stub(fw["tim6_audio_zero_cross_period"], lambda e: zc.append(1))
    got = []

    def parser(e):                                # stub stock parser: record what it was handed
        if e.r8(fw["aprs_rx_frame_ready"]):
            n = e.r8(fw["aprs_rx_frame_len"])
            raw = e.read(fw["aprs_rx_frame"], 300)
            got.append((raw[:n + 5], n))             # n = index of last info byte; then FCS, '~', NUL
            e.w8(fw["aprs_rx_frame_ready"], 0)
    e.stub(fw["aprs_rx_frame_process"], parser)
    fcs_stub = []
    e.stub(fw["ax25_fcs_check"], lambda e: fcs_stub.append(1) or 0)

    # garbage RAM at boot: p14 must not deliver anything before aprs_sigdec initialised the slice
    e.write(m["ram"], b"\xA5" * m["ram_len"])
    e.w8(fw["ana_rx_audio_open"], 1)
    e.w16(fw["aprs_tx_holdoff"], 0)
    e.call(s["aprs_p14"])
    assert not got, "delivered garbage before init"
    assert e.call(s["aprs_fcs"], 0x20000000, 0) == 0 and fcs_stub == [1], "fcs must defer to stock before init"

    # sigdec: digital channel / APRS off -> no start; analog + APRS on -> mode 5 at 9600 Hz
    digital[0] = 1
    e.call(s["aprs_sigdec"])
    assert e.r8(fw["tim6_audio_mode"]) == 0 and not rate
    digital[0] = 0
    e.w8(REC + 0x21, 0x00)
    e.call(s["aprs_sigdec"])
    assert e.r8(fw["tim6_audio_mode"]) == 0 and not rate
    e.w8(REC + 0x21, 0x20)
    e.call(s["aprs_sigdec"])
    assert e.r8(fw["tim6_audio_mode"]) == 5, e.r8(fw["tim6_audio_mode"])
    assert rate == [(12499, 0)] and cmd == [(0x40001000, 1)], (rate, cmd)

    # TIM6 dispatch: mode 3 still reaches the stock handler
    e.run_from(0x08004A4C, regs=dict(r0=3), stack=(0x4444, 0x08000101))
    assert zc == [1]

    # demod: three frames (one with 0x7E in the info, one with de-emphasis tilt), 8-bit quantised
    f1 = A.ax25_ui("N0CALL-9", "APRS", ["WIDE1-1", "WIDE2-1"], b"!4903.50N/07201.75W-aprs_rx test")
    f2 = A.ax25_ui("TEST-1", "T2SP0W", ["W2KCH-2*"], b"`jK'l ,k/`\"5u}tilde~inside_")
    f3 = A.ax25_ui("VE3XYZ-7", "APOG77", [], b">tilted audio test 0123456789")
    audio = [0.0] * 960
    for f, tilt in ((f1, 1.0), (f2, 0.8), (f3, 0.55)):
        audio += A.modulate(f, 9600, amp=0.45, noise=0.04, tilt=tilt) + [0.0] * 1500
    adc = fw["adc_pc0_rx_audio"]
    for i, v in enumerate(audio):
        q = min(255, max(0, int(128 + 120 * v)))
        e.w16(adc, q << 4)
        e.run_from(0x08004A4C, regs=dict(r0=5), stack=(0x4444, 0x08000101))
        if i % 96 == 95:                          # p14 runs every 10 ms
            e.call(s["aprs_p14"])
    want = [b"+APRS:~" + f + b"~\0" for f in (f1, f2, f3)]
    assert [g for g, _ in got] == want, [g for g, _ in got]
    assert [n for _, n in got] == [len(f) + 4 for f in (f1, f2, f3)]
    assert struct.unpack_from("<II", e.read(m["ram"] + OFF_FRAMES, 8)) == (3, 0)
    # our frames bypass the stock FCS scan (f2 has 0x7E in the info)
    e.w8(m["ram"] + 25, 1)                         # ours
    assert e.call(s["aprs_fcs"], fw["aprs_rx_frame"] + 7, 7) == 1

    # acceptance conditions: squelch closed -> frame dropped, not delivered later
    e.w8(m["ram"] + 25, 0)
    got.clear()
    e.w8(fw["ana_rx_audio_open"], 0)
    for i, v in enumerate([0.0] * 500 + A.modulate(f1, 9600, amp=0.45) + [0.0] * 2500):
        e.w16(adc, min(255, max(0, int(128 + 120 * v))) << 4)
        e.run_from(0x08004A4C, regs=dict(r0=5), stack=(0x4444, 0x08000101))
        if i % 96 == 95:
            e.call(s["aprs_p14"])
    e.w8(fw["ana_rx_audio_open"], 1)
    e.call(s["aprs_p14"])
    assert not got, "frame delivered with squelch closed"
    return "init guard, sigdec gating, mode-3 passthrough, 3 frames (0x7E, tilt) delivered once each, FCS bypass, squelch gating"
