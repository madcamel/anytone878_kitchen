#!/usr/bin/env python3
"""Add a RAM peek/poke/call debug channel to the D878UV V4.00 PC link (on top of band-unlock r1).

    .venv/bin/python -I patches/mkpatch_debug.py   (after patches/mkpatch_bandunlock.py uv)

Hook: usb_pc_link_step's only `bl pc_cmd_dispatch` (0x08081186) becomes `bl dbg_hook`.
dbg_hook looks at the request in 0x2000D5A8 (length u16 at 0x20001090). If byte1 is 'd' and byte0 is
D0/D1/D2 with the exact expected length, it handles it and replies via FUN_0807CFB2(len) (the
dispatcher's own reply path); anything else tail-calls the original pc_cmd_dispatch unchanged.
It runs before the session gate, so it works in normal operation (no PROGRAM, no PC-MODE screen,
no END reboot). Boot path and PTT+PF3 update mode are untouched (they run before any USB traffic).

Wire format (buffer is word aligned, so fields at +4/+8 are aligned):
  PEEK  D0 'd' N 0 | addr32            (len 8)    -> D0 N 0 0 | data[N] | sum8(all prior bytes)
  POKE  D1 'd' N 0 | addr32 | data[N]  (len 8+N)  -> D1 06
  CALL  D2 'd' 0 0 | fn32 | a0 a1 a2 a3 (len 24)  -> D2 06 0 0 | r0 | r1
N=4 / N=2 on an aligned address use one word / halfword access (peripheral-register safe);
otherwise bytes. No address checking on the device: the host tool (tools/radio.py) enforces ranges.
Code lives in dead StdPeriph code TIM_ICInit+TIM_PWMIConfig, 0x080065C8-0x0800671C only: live TIM
functions keep literal pools at 0x080065C4 and 0x0800671C-0x0800672F (the reference sweep enforces this).
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

ROOT = hcfg.ROOT
BASE = hcfg.BASE
STOCK = hcfg.fw_path("uv")
STOCK_SHA = hcfg.MODELS["uv"]["sha256"]["CDD"]
IN = hcfg.OUT / "D878UV_V4.00_bandunlock_r1/D878UV_V4.00_bandunlock_r1.bin"
IN_SHA = "885864f8ca9e109cf2c5802b2ee503d0b4c5d89f2eb3174ab1c8b3535b8512d2"
OUT = hcfg.OUT / "D878UV_V4.00_bandunlock_r1_dbg1"
STEM = "D878UV_V4.00_20250725"
BINNAME = "D878UV_V4.00_bandunlock_r1_dbg1.bin"

DEAD = (0x0800659E, 0x08006730)  # TIM_ICInit + TIM_PWMIConfig (no callers)
FALSE_POS = (0x0800644E, 0x080064EA)
HOOK_SITE = 0x08081186          # bl pc_cmd_dispatch inside usb_pc_link_step
HOOK_BYTES = bytes.fromhex("fef752fd")
DISPATCH = 0x0807FC2E           # pc_cmd_dispatch
REPLY = 0x0807CFB2              # FUN_0807CFB2(len): usb_cdc_tx(handle, len) of 0x2000D5A8
BUF, LEN = 0x2000D5A8, 0x20001090
HOLE = (0x080065C8, 0x0800671C)  # inside TIM_ICInit+TIM_PWMIConfig (unreferenced), between live literal pools at 0x080065C4 and 0x0800671C-0x0800672F

SRC = f"""
    push {{r4, r5, r6, r7, lr}}
    movw r4, #{BUF & 0xffff}
    movt r4, #{BUF >> 16}
    movw r5, #{LEN & 0xffff}
    movt r5, #{LEN >> 16}
    ldrh r5, [r5]
    ldrb r0, [r4, #1]
    cmp r0, #0x64
    bne passthru
    ldrb r0, [r4]
    ldrb r6, [r4, #2]
    ldr r7, [r4, #4]
    cmp r0, #0xD0
    beq peek
    cmp r0, #0xD1
    beq poke
    cmp r0, #0xD2
    beq call
passthru:
    pop.w {{r4, r5, r6, r7, lr}}
    b.w {DISPATCH:#x}

peek:
    cmp r5, #8
    bne passthru
    cmp r6, #0
    beq passthru
    cmp r6, #4
    bne pk_h
    tst r7, #3
    bne pk_h
    ldr r0, [r7]
    str r0, [r4, #4]
    b pk_done
pk_h:
    cmp r6, #2
    bne pk_b
    tst r7, #1
    bne pk_b
    ldrh r0, [r7]
    strh r0, [r4, #4]
    b pk_done
pk_b:
    movs r1, #0
    adds r2, r4, #4
pk_loop:
    ldrb r0, [r7, r1]
    strb r0, [r2, r1]
    adds r1, #1
    cmp r1, r6
    blo pk_loop
pk_done:
    strb r6, [r4, #1]
    movs r0, #0
    strb r0, [r4, #2]
    strb r0, [r4, #3]
    adds r2, r6, #4
    movs r1, #0
    movs r3, #0
ck:
    ldrb r0, [r4, r1]
    adds r3, r3, r0
    adds r1, #1
    cmp r1, r2
    blo ck
    strb r3, [r4, r2]
    adds r0, r2, #1
    b send

poke:
    adds r0, r6, #8
    cmp r5, r0
    bne passthru
    cmp r6, #0
    beq passthru
    cmp r6, #4
    bne po_h
    tst r7, #3
    bne po_h
    ldr r0, [r4, #8]
    str r0, [r7]
    b po_done
po_h:
    cmp r6, #2
    bne po_b
    tst r7, #1
    bne po_b
    ldrh r0, [r4, #8]
    strh r0, [r7]
    b po_done
po_b:
    movs r1, #0
    adds r2, r4, #8
po_loop:
    ldrb r0, [r2, r1]
    strb r0, [r7, r1]
    adds r1, #1
    cmp r1, r6
    blo po_loop
po_done:
    movs r0, #6
    strb r0, [r4, #1]
    movs r0, #2
    b send

call:
    cmp r5, #24
    bne passthru
    orr r7, r7, #1
    ldr r0, [r4, #8]
    ldr r1, [r4, #12]
    ldr r2, [r4, #16]
    ldr r3, [r4, #20]
    blx r7
    str r0, [r4, #4]
    str r1, [r4, #8]
    movs r0, #6
    strb r0, [r4, #1]
    movs r0, #0
    strb r0, [r4, #2]
    strb r0, [r4, #3]
    movs r0, #12
send:
    bl {REPLY:#x}
    pop.w {{r4, r5, r6, r7, pc}}
"""


def sha(b):
    return hashlib.sha256(b).hexdigest()


def asm(src, addr):
    enc, _ = Ks(KS_ARCH_ARM, KS_MODE_THUMB).asm(src, addr)
    return bytes(enc)


class Emu:
    """Run dbg_hook with stubbed dispatch/reply and a scratch target region."""
    STOP, SP = 0x08000100, 0x20017F00

    def __init__(self, img):
        u = self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0x08000000, 0x100000)
        u.mem_write(BASE, img)
        u.mem_map(0x20000000, 0x18000)
        u.mem_map(0x40000000, 0x1000)   # a "peripheral" page for width tests
        self.calls = []
        u.hook_add(UC_HOOK_CODE, self._hook, begin=0x08000000, end=0x080FFFFF)

    def _hook(self, u, addr, size, _):
        if addr in (DISPATCH, REPLY, 0x08000200):
            r0 = u.reg_read(A.UC_ARM_REG_R0)
            r1 = u.reg_read(A.UC_ARM_REG_R1)
            self.calls.append((addr, r0, r1))
            if addr == 0x08000200:          # test "function": return a0+a1, a2^a3
                u.reg_write(A.UC_ARM_REG_R0, (r0 + r1) & 0xffffffff)
                u.reg_write(A.UC_ARM_REG_R1, u.reg_read(A.UC_ARM_REG_R2) ^ u.reg_read(A.UC_ARM_REG_R3))
            lr = u.reg_read(A.UC_ARM_REG_LR)
            u.reg_write(A.UC_ARM_REG_PC, lr)  # return (thumb bit kept in lr)

    def run(self, entry, req):
        u = self.u
        u.mem_write(BUF, bytes(req) + bytes(0x100))
        u.mem_write(LEN, struct.pack("<H", len(req)))
        self.calls = []
        u.reg_write(A.UC_ARM_REG_SP, self.SP)
        for r in (A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R6, A.UC_ARM_REG_R7):
            u.reg_write(r, 0xA5A50000 + r)
        u.reg_write(A.UC_ARM_REG_LR, self.STOP | 1)
        u.emu_start(entry | 1, self.STOP, count=20000)
        for r in (A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R6, A.UC_ARM_REG_R7):
            assert u.reg_read(r) == 0xA5A50000 + r, "callee-saved reg clobbered"
        assert u.reg_read(A.UC_ARM_REG_SP) == self.SP, "stack imbalance"
        return self.calls, bytes(u.mem_read(BUF, 0x120))


def test(img, entry):
    e = Emu(img)
    u = e.u
    pat = bytes(range(0x40, 0x40 + 64))
    u.mem_write(0x20010000, pat)
    u.mem_write(0x40000010, struct.pack("<I", 0x12345678))

    def req(cmd, n, addr, data=b""):
        return bytes([cmd, 0x64, n, 0]) + struct.pack("<I", addr) + data

    # peek bytes
    c, b = e.run(entry, req(0xD0, 37, 0x20010003))
    assert c == [(REPLY, 37 + 5, c[0][2])], c
    assert b[0] == 0xD0 and b[1] == 37 and b[4:4 + 37] == pat[3:40]
    assert b[4 + 37] == sum(b[:4 + 37]) & 0xff
    # peek word (aligned, peripheral) and halfword
    c, b = e.run(entry, req(0xD0, 4, 0x40000010))
    assert c[0][:2] == (REPLY, 9) and b[4:8] == struct.pack("<I", 0x12345678)
    c, b = e.run(entry, req(0xD0, 2, 0x40000012))
    assert c[0][:2] == (REPLY, 7) and b[4:6] == b"\x34\x12"
    # poke bytes, word
    c, b = e.run(entry, req(0xD1, 5, 0x20011001, b"ABCDE"))
    assert c[0][:2] == (REPLY, 2) and b[:2] == b"\xd1\x06"
    assert bytes(u.mem_read(0x20011000, 7)) == b"\x00ABCDE\x00"
    c, b = e.run(entry, req(0xD1, 4, 0x40000020, struct.pack("<I", 0xCAFEBABE)))
    assert bytes(u.mem_read(0x40000020, 4)) == struct.pack("<I", 0xCAFEBABE)
    # call (fn without thumb bit; hook orrs it)
    c, b = e.run(entry, req(0xD2, 0, 0x08000200, struct.pack("<4I", 5, 7, 0xF0, 0x0F)))
    assert [x[0] for x in c] == [0x08000200, REPLY] and c[1][1] == 12, c
    assert b[:2] == b"\xd2\x06" and struct.unpack_from("<II", b, 4) == (12, 0xFF)
    # pass-through cases: stock commands, wrong length, wrong magic
    for r in (b"PROGRAM", b"R\x02\xfa\x00\x00\x10", req(0xD0, 4, 0x20010000) + b"x",
              req(0xD1, 4, 0x20010000, b"ab"), bytes([0xD0, 0x65, 4, 0, 0, 0, 1, 0x20]),
              req(0xD0, 0, 0x20010000), req(0xD2, 0, 0x08000200)):
        c, _ = e.run(entry, r)
        assert [x[0] for x in c] == [DISPATCH], (r, c)
    assert bytes(u.mem_read(0x20010000, 64)) == pat  # passthroughs touched nothing
    print("emulation: peek/poke/call/passthrough OK")


def main():
    stock = STOCK.read_bytes()
    img = bytearray(IN.read_bytes())
    assert sha(stock) == STOCK_SHA, "stock image mismatch"
    assert sha(img) == IN_SHA, "band-unlock r1 image mismatch (rebuild with mkpatch_bandunlock.py uv)"
    lo, hi = HOLE
    o = lambda a: a - BASE
    assert img[o(lo):o(hi)] == stock[o(lo):o(hi)], "hole already modified"
    # sources inside the dead functions themselves don't count; two halfword-sweep hits are the 2nd
    # halfword of `cmp.w r0,#0x40000000` (b0f1 804f) in live TI3_Config/TI2_Config, not ldr's.
    bad = SIG.unsafe_refs(bytes(img), [HOLE], exclude=[DEAD])
    for fp in FALSE_POS:
        assert img[o(fp) - 2:o(fp) + 2] == bytes.fromhex("b0f1804f"), "false-positive site changed"
    bad = [b for b in bad if int(b[0], 16) not in FALSE_POS]
    assert not bad, "references into hole: %s" % [tuple(hex(y) if isinstance(y, int) else y for y in x) for x in bad[:10]]
    assert img[o(HOOK_SITE):o(HOOK_SITE) + 4] == HOOK_BYTES, "hook site mismatch"

    code = asm(SRC, lo)
    assert len(code) <= hi - lo, "handler too big: %d > %d" % (len(code), hi - lo)
    img[o(lo):o(lo) + len(code)] = code
    img[o(HOOK_SITE):o(HOOK_SITE) + 4] = asm("bl %#x" % lo, HOOK_SITE)
    print("dbg_hook @ %#x, %d bytes (%d free)" % (lo, len(code), hi - lo - len(code)))
    for i in Cs(CS_ARCH_ARM, CS_MODE_THUMB).disasm(bytes(img[o(HOOK_SITE):o(HOOK_SITE) + 4]), HOOK_SITE):
        print("hook: %#x %s %s" % (i.address, i.mnemonic, i.op_str))

    test(bytes(img), lo)
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
