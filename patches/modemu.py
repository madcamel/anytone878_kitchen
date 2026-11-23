"""Unicorn harness for mod tests: the whole built image (incl. the mod area) plus RAM.

    e = Emu(img)                 # img = full patched image as loaded at 0x08004000
    e.stub(addr, fn)             # replace a firmware function: fn(emu) -> optional r0; then returns
    e.call(fn_addr, *args)       # run a function to completion (Thumb bit optional), returns r0
    e.run_from(addr, stack=[...])  # start mid-function (hook sites); stops when pc reaches STOP
    e.r8/r16/r32(addr), e.w8/w16/w32(addr, v), e.read/write(addr, bytes)
"""
import struct

from unicorn import UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE, Uc
import unicorn.arm_const as A

BASE = 0x08004000
STOP = 0x08000100
SP = 0x20015700           # below the mod RAM pool


class Emu:
    def __init__(self, img):
        u = self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0x08000000, 0x100000)
        u.mem_write(BASE, bytes(img))
        u.mem_map(0x20000000, 0x18000)
        u.mem_map(0x40000000, 0x30000)     # APB peripherals (writes land in plain memory)
        u.mem_map(0xE0000000, 0x100000)    # SCS
        self.stubs = {}
        self.calls = []

    def stub(self, addr, fn):
        addr &= ~1
        self.stubs[addr] = fn
        self.u.hook_add(UC_HOOK_CODE, self._stub, begin=addr, end=addr)

    def _stub(self, u, addr, size, _):
        fn = self.stubs.get(addr)
        if fn is None:
            return
        self.calls.append(addr)
        r = fn(self)
        if r is not None:
            u.reg_write(A.UC_ARM_REG_R0, r & 0xFFFFFFFF)
        u.reg_write(A.UC_ARM_REG_PC, u.reg_read(A.UC_ARM_REG_LR))

    def arg(self, i):
        return self.u.reg_read((A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3)[i])

    def call(self, fn, *args, count=2_000_000):
        u = self.u
        for r, v in zip((A.UC_ARM_REG_R0, A.UC_ARM_REG_R1, A.UC_ARM_REG_R2, A.UC_ARM_REG_R3), args):
            u.reg_write(r, v & 0xFFFFFFFF)
        for r in (A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R6, A.UC_ARM_REG_R7,
                  A.UC_ARM_REG_R8, A.UC_ARM_REG_R9, A.UC_ARM_REG_R10, A.UC_ARM_REG_R11):
            u.reg_write(r, 0xC0DE0000 + r)
        u.reg_write(A.UC_ARM_REG_SP, SP)
        u.reg_write(A.UC_ARM_REG_LR, STOP | 1)
        u.emu_start(fn | 1, STOP, count=count)
        for r in (A.UC_ARM_REG_R4, A.UC_ARM_REG_R5, A.UC_ARM_REG_R6, A.UC_ARM_REG_R7,
                  A.UC_ARM_REG_R8, A.UC_ARM_REG_R9, A.UC_ARM_REG_R10, A.UC_ARM_REG_R11):
            assert u.reg_read(r) == 0xC0DE0000 + r, "callee-saved register clobbered"
        assert u.reg_read(A.UC_ARM_REG_SP) == SP, "stack imbalance"
        return u.reg_read(A.UC_ARM_REG_R0)

    def run_from(self, addr, regs=None, stack=(), count=200_000):
        """Start at addr (mid-function) with regs and words pushed on the stack; the code must
        eventually return to STOP (put STOP|1 where a pop {.., pc} will find it)."""
        u = self.u
        sp = SP - 4 * len(stack)
        u.mem_write(sp, struct.pack("<%dI" % len(stack), *stack))
        u.reg_write(A.UC_ARM_REG_SP, sp)
        for r, v in (regs or {}).items():
            u.reg_write(getattr(A, "UC_ARM_REG_" + r.upper()), v)
        u.emu_start(addr | 1, STOP, count=count)
        assert u.reg_read(A.UC_ARM_REG_SP) == SP, "stack imbalance"

    def read(self, a, n):
        return bytes(self.u.mem_read(a, n))

    def write(self, a, b):
        self.u.mem_write(a, bytes(b))

    def r8(self, a): return self.read(a, 1)[0]
    def r16(self, a): return struct.unpack("<H", self.read(a, 2))[0]
    def r32(self, a): return struct.unpack("<I", self.read(a, 4))[0]
    def w8(self, a, v): self.write(a, bytes([v & 0xFF]))
    def w16(self, a, v): self.write(a, struct.pack("<H", v & 0xFFFF))
    def w32(self, a, v): self.write(a, struct.pack("<I", v & 0xFFFFFFFF))
