"""capture: squelch-triggered RX-audio recorder for APRS RX development (host: tools/aprscap.py).
Either/or with aprs_rx: both take the TIM6 mode dispatch."""
import struct

NAME = "capture"
VERSION = 1
RAM = 16 + 10416                  # header + ring (1.085 s at 9.6 kHz)
SOURCES = ["capture.c"]
HOOKS = [
    dict(site=0x08004A4C, orig="032801d106f092f8", asm="nop\nnop\nbl {cap_tim6}",
         why="TIM6_IRQHandler: `cmp r0,#3; bne done; bl zero_cross` -> every mode except 1/2 reaches cap_tim6(mode)"),
]


def test(e, m, fw):
    """e: modemu.Emu on the built image; m: this mod's layout (ram, ram_len, syms); fw: name->addr."""
    ram, n = m["ram"], m["ram_len"] - 16
    MODE, ADC, SQ = fw["tim6_audio_mode"], fw["adc_pc0_rx_audio"], fw["ana_sq_open"]
    zc = []
    e.stub(fw["tim6_audio_zero_cross_period"], lambda e: zc.append(1))
    e.write(ram, bytes(16))

    def tick(mode, sample, sq):
        e.w8(MODE, mode); e.w16(ADC, sample); e.w8(SQ, sq)
        # enter at the hook site as TIM6_IRQHandler does: r0 = mode, {r4, pc} on the stack
        e.run_from(0x08004A4C, regs=dict(r0=mode), stack=(0x4444, 0x08000101))

    def hdr():
        st, wr, left, trig, seq, ticks = struct.unpack("<BxHHHII", e.read(ram, 16))
        return dict(state=st, wr=wr, left=left, trig=trig, seq=seq, ticks=ticks)
    PRE, TAIL = 1920, 480
    POST = n - PRE
    for mode in (0, 5):
        tick(mode, 0x800, 0)
    assert hdr()["ticks"] == 0 and not zc
    tick(3, 0x800, 0)
    assert zc == [1] and hdr()["ticks"] == 0
    for i in range(n + 5):
        tick(4, (i & 0xFF) << 4, 0)
    h = hdr()
    assert h["state"] == 0 and h["wr"] == 5 and h["ticks"] == n + 5, h
    assert e.read(ram + 16, 5) == bytes((n + i) & 0xFF for i in range(5))
    tick(4, 0x100, 1)
    h = hdr()
    assert h["state"] == 1 and h["trig"] == 6 and h["left"] == POST, h
    for _ in range(POST):
        tick(4, 0x200, 1)
    h = hdr()
    assert h["state"] == 2 and h["seq"] == 1 and h["wr"] == (6 + POST) % n, h
    tick(4, 0xFF0, 1)
    assert hdr()["wr"] == h["wr"] and hdr()["ticks"] == h["ticks"] + 1
    e.w8(ram, 0)
    for _ in range(101):
        tick(4, 0x300, 1)
    for _ in range(TAIL - 1):
        tick(4, 0x300, 0)
    assert hdr()["state"] == 1
    tick(4, 0x300, 0)
    h = hdr()
    assert h["state"] == 2 and h["seq"] == 2, h
    return "mode 3 passthrough, ring wrap, trigger, POST freeze, squelch-close TAIL"
