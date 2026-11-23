#!/usr/bin/env python3
"""Add a squelch-triggered RX-audio recorder (TIM6 mode 4) on top of the debug channel image.

    .venv/bin/python -I patches/mkpatch_capture.py   (after patches/mkpatch_debug.py)

Purpose: record real APRS bursts from PC0 (pre-squelch RX audio, ADC1 ch10, always fresh via DMA)
so an AFSK1200 demodulator can be developed and regression-tested offline. Host side:
tools/aprscap.py (arms, re-arms after retunes, saves WAVs).

Hook: in TIM6_IRQHandler the `cmp r0,#3; bne done; bl tim6_audio_zero_cross_period` (0x08004A4C)
becomes `nop; nop; bl cap_isr`, so cap_isr gets every mode other than 1/2 in r0:
  mode 3 -> tail-call the stock zero-cross handler (unchanged behaviour), mode 4 -> record, else return.
Nothing enters mode 4 by itself: the host pokes tim6_audio_mode=4 and calls tim6_set_rate/TIM_Cmd.
rf_cmd_apply_lo (any retune) and TX stop TIM6 and reset the mode, so recording stops cleanly then.

Capture block CAP (free RAM above the initial SP):
  +0 u8  state   0 recording into the ring, waiting for squelch; 1 triggered, counting down; 2 frozen
  +2 u16 wr      next ring index
  +4 u16 left    samples still to record after the trigger
  +6 u16 trig    ring index at the trigger
  +8 u32 seq     completed captures
  +12 u32 ticks  ISR calls in mode 4 (host derives the real sample rate)
  +16 u8 buf[N]  8-bit unsigned samples (ADC >> 4)
Trigger = ana_sq_open (analog squelch incl. CTCSS/DCS). After the trigger it records POST samples,
cut to TAIL samples once the squelch closes again, so short bursts don't wait for the buffer.
The host re-arms by writing state=0.
Code lives in dead StdPeriph FLASH option-byte functions 0x080078EA-0x08007B28 (no references).
"""
import hashlib
import shutil
import struct
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from keystone import KS_ARCH_ARM, KS_MODE_THUMB, Ks
from unicorn import UC_ARCH_ARM, UC_MODE_THUMB, Uc, UC_HOOK_CODE
import unicorn.arm_const as A

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import bandunlock_sigs as SIG  # noqa: E402
import hcfg  # noqa: E402

BASE = hcfg.BASE
STOCK = hcfg.fw_path("uv")
STOCK_SHA = hcfg.MODELS["uv"]["sha256"]["CDD"]
IN = hcfg.OUT / "D878UV_V4.00_bandunlock_r1_dbg1/D878UV_V4.00_bandunlock_r1_dbg1.bin"
IN_SHA = "257c2af64a8d4ae9dfbae8580fefd3c5566f59f407bc9859376084df69893376"
OUT = hcfg.OUT / "D878UV_V4.00_bandunlock_r1_dbg1_cap1"
STEM = "D878UV_V4.00_20250725"
BINNAME = "D878UV_V4.00_bandunlock_r1_dbg1_cap1.bin"

HOLE = (0x080078EC, 0x08007B28)   # FLASH_ProgramOptionByteData..FLASH_UserOptionByteConfig (dead)
DEAD = (0x080078EA, 0x08007B28)
HOOK = 0x08004A4C                 # cmp r0,#3 ; bne 0x08004A54 ; bl tim6_audio_zero_cross_period
HOOK_BYTES = bytes.fromhex("032801d106f092f8")
ZEROCROSS = 0x0800AB78            # tim6_audio_zero_cross_period
ADC = 0x20001048                  # adc_pc0_rx_audio (u16, 12-bit)
SQ = 0x2000003E                   # ana_sq_open
MODE = 0x20000178                 # tim6_audio_mode (host side; listed for aprscap.py)
CAP = 0x20015740
BUF = CAP + 16
N = 0x20018000 - BUF              # 10416 samples = 1.085 s at 9.6 kHz
RATE = 9600
PRE = 1920                        # ~200 ms kept before the trigger
POST = N - PRE
TAIL = 480                        # ~50 ms after squelch closes

SRC = f"""
cap_isr:
    cmp r0, #3
    bne not3
    b.w {ZEROCROSS:#x}
not3:
    cmp r0, #4
    bne ret
    movw r3, #{CAP & 0xffff}
    movt r3, #{CAP >> 16}
    ldr r0, [r3, #12]
    adds r0, #1
    str r0, [r3, #12]
    ldrb r1, [r3]
    cmp r1, #2
    beq ret
    movw r2, #{ADC & 0xffff}
    movt r2, #{ADC >> 16}
    ldrh r2, [r2]
    lsrs r2, r2, #4
    ldrh r0, [r3, #2]
    add.w r12, r3, #16
    strb.w r2, [r12, r0]
    adds r0, #1
    movw r2, #{N}
    cmp r0, r2
    blo nowrap
    movs r0, #0
nowrap:
    strh r0, [r3, #2]
    movw r2, #{SQ & 0xffff}
    movt r2, #{SQ >> 16}
    ldrb r2, [r2]
    cmp r1, #0
    bne counting
    cbz r2, ret
    movs r1, #1
    strb r1, [r3]
    strh r0, [r3, #6]
    movw r1, #{POST}
    strh r1, [r3, #4]
    bx lr
counting:
    ldrh r1, [r3, #4]
    cbnz r2, dec
    cmp r1, #{TAIL}
    bls dec
    movw r1, #{TAIL}
dec:
    subs r1, #1
    strh r1, [r3, #4]
    bne ret
    movs r1, #2
    strb r1, [r3]
    ldr r1, [r3, #8]
    adds r1, #1
    str r1, [r3, #8]
ret:
    bx lr
"""


def sha(b):
    return hashlib.sha256(b).hexdigest()


def asm(src, addr):
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(src, addr)
    return bytes(enc)


class Emu:
    """Run TIM6_IRQHandler's patched tail (from the mode load at 0x08004A48) per sample."""
    STOP, SP = 0x08000100, 0x20015700

    def __init__(self, img):
        u = self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0x08000000, 0x100000)
        u.mem_write(BASE, img)
        u.mem_map(0x20000000, 0x18000)
        self.zc = 0
        u.hook_add(UC_HOOK_CODE, self._hook, begin=ZEROCROSS, end=ZEROCROSS)

    def _hook(self, u, addr, size, _):
        self.zc += 1
        u.reg_write(A.UC_ARM_REG_PC, u.reg_read(A.UC_ARM_REG_LR))

    def tick(self, mode, sample, sq):
        u = self.u
        u.mem_write(MODE, bytes([mode]))
        u.mem_write(ADC, struct.pack("<H", sample))
        u.mem_write(SQ, bytes([sq]))
        u.reg_write(A.UC_ARM_REG_SP, self.SP)
        u.reg_write(A.UC_ARM_REG_R4, 0x4444)
        # enter at the patched mode-3 compare: the handler has just loaded r0 = mode there
        u.reg_write(A.UC_ARM_REG_R0, mode)
        u.reg_write(A.UC_ARM_REG_LR, self.STOP | 1)
        # 0x08004A4C..: nop nop bl cap_isr ; then 0x08004A54 pop {r4,pc} -> needs a frame
        u.mem_write(self.SP, struct.pack("<II", 0x4444, self.STOP | 1))
        u.emu_start(HOOK | 1, self.STOP, count=500)
        assert u.reg_read(A.UC_ARM_REG_SP) == self.SP + 8, "stack imbalance"

    def cap(self):
        st, wr, left, trig, seq, ticks = struct.unpack("<BxHHHII", bytes(self.u.mem_read(CAP, 16)))
        return dict(state=st, wr=wr, left=left, trig=trig, seq=seq, ticks=ticks)


def test(img):
    e = Emu(img)
    # modes 0/5: nothing happens; mode 3 still reaches the stock zero-cross handler
    for m in (0, 5):
        e.tick(m, 0x800, 0)
    assert e.cap()["ticks"] == 0 and e.zc == 0
    e.tick(3, 0x800, 0)
    assert e.zc == 1 and e.cap()["ticks"] == 0
    # mode 4: ring recording, wraps, sample = adc >> 4
    for i in range(N + 5):
        e.tick(4, (i & 0xff) << 4, 0)
    c = e.cap()
    assert c["state"] == 0 and c["wr"] == 5 and c["ticks"] == N + 5, c
    assert bytes(e.u.mem_read(BUF, 5)) == bytes(((N + i) & 0xff) for i in range(5))
    # squelch opens -> trigger, stays open past POST -> freezes after exactly POST samples
    e.tick(4, 0x100, 1)
    c = e.cap()
    assert c["state"] == 1 and c["trig"] == 6 and c["left"] == POST, c
    for _ in range(POST):
        e.tick(4, 0x200, 1)
    c = e.cap()
    assert c["state"] == 2 and c["seq"] == 1 and c["wr"] == (6 + POST) % N, c
    wr = c["wr"]
    e.tick(4, 0xFF0, 1)                       # frozen: buffer untouched, ticks still count
    assert e.cap()["wr"] == wr and e.cap()["ticks"] == c["ticks"] + 1
    # re-arm, short burst: squelch closes -> only TAIL more samples
    e.u.mem_write(CAP, b"\x00")
    e.tick(4, 0x300, 1)
    for _ in range(100):
        e.tick(4, 0x300, 1)
    for _ in range(TAIL - 1):
        e.tick(4, 0x300, 0)
    assert e.cap()["state"] == 1
    e.tick(4, 0x300, 0)
    c = e.cap()
    assert c["state"] == 2 and c["seq"] == 2, c
    print("emulation: mode 3 passthrough, ring wrap, trigger, POST freeze, squelch-close TAIL OK")


def main():
    stock = STOCK.read_bytes()
    img = bytearray(IN.read_bytes())
    assert sha(stock) == STOCK_SHA, "stock image mismatch"
    assert sha(img) == IN_SHA, "debug image mismatch (rebuild with mkpatch_debug.py)"
    o = lambda a: a - BASE
    lo, hi = HOLE
    assert img[o(lo):o(hi)] == stock[o(lo):o(hi)], "hole already modified"
    bad = SIG.unsafe_refs(bytes(img), [DEAD], exclude=[DEAD])
    assert not bad, "references into hole: %s" % bad[:10]
    assert img[o(HOOK):o(HOOK) + 8] == HOOK_BYTES, "hook site mismatch"

    code = asm(SRC, lo)
    assert len(code) <= hi - lo, "cap_isr too big: %d > %d" % (len(code), hi - lo)
    img[o(lo):o(lo) + len(code)] = code
    img[o(HOOK):o(HOOK) + 8] = asm("nop\nnop\nbl %#x" % lo, HOOK)
    print("cap_isr @ %#x, %d bytes (%d free in hole)" % (lo, len(code), hi - lo - len(code)))
    for i in Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(bytes(img[o(HOOK) - 4:o(HOOK) + 10]), HOOK - 4):
        print("hook: %#x %s %s" % (i.address, i.mnemonic, i.op_str))

    test(bytes(img))
    assert len(img) == len(stock), "length changed"
    diff = sum(1 for a, b in zip(img, stock) if a != b)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / BINNAME).write_bytes(img)
    (OUT / (STEM + ".CDD")).write_bytes(img)
    for ext in (".CDI", ".spi"):
        shutil.copy(STOCK.with_suffix(ext), OUT / (STEM + ext))
    print("wrote %s  sha256 %s  (%d bytes differ from stock)" % (OUT / BINNAME, sha(img), diff))


if __name__ == "__main__":
    main()
