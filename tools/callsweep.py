#!/usr/bin/env python3
"""callsweep.py TARGET... : every `bl TARGET` in the image, with the immediate values of r0-r3 at the
call (tracked from preceding mov/movs/movw in the same basic block; '?' if not a constant), grouped by
the calling function. Good for "which registers does at1846_write_reg get" style questions:
    callsweep.py 0x0807A6FC           (AT1846S register writes)
Library use: sweep(img, {addr,...}) -> [(site, target, {"r0": v, ...}), ...]
"""
import sys

from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM, ARM_OP_REG

import hcfg


def sweep(img, targets, base=hcfg.BASE, end=hcfg.CODE_END):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    md.detail = True
    out = []
    o = 0
    while o < end - base:
        ins = list(md.disasm(img[o:o + 0x400], base + o))
        if not ins:
            o += 2
            continue
        last = {}
        for i in ins:
            if i.mnemonic in ("movs", "mov", "mov.w", "movw") and len(i.operands) == 2 \
                    and i.operands[0].type == ARM_OP_REG and i.operands[1].type == ARM_OP_IMM:
                last[i.reg_name(i.operands[0].reg)] = i.operands[1].imm
            elif i.mnemonic == "bl":
                t = i.operands[0].imm
                if t in targets:
                    out.append((i.address, t, {r: last.get(r) for r in ("r0", "r1", "r2", "r3")}))
                last = {}
            elif i.mnemonic.startswith(("b", "pop", "bx")):
                last = {}
        o = ins[-1].address + ins[-1].size - base
    return out


if __name__ == "__main__":
    import gh
    targets = {int(x, 0) for x in sys.argv[1:]}
    cache = {}
    for site, t, regs in sweep(hcfg.load_fw(), targets):
        try:
            f = cache.get(site) or gh.function(site)
            fn = "%s@%s" % (f["name"], f["address"])
        except gh.ApiError:
            fn = "(no function)"
        args = " ".join("%s=%s" % (r, "?" if v is None else hex(v)) for r, v in regs.items())
        print("%08x -> %08x  %-40s %s" % (site, t, fn, args))
