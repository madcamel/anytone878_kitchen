"""Locate band-unlock patch points in a D878UV-family image by signature (no Ghidra needed).

find_sites(img) -> (table_sites, dmr_sites):
  table site = `ldr Rb,[pc,#]` (literal BASE MHz) ; `subs Rd,Rs,Rb` ; `ldr Rx,[pc,#]` (STEP) ; `udiv`
  dmr site   = `ldr r1,[pc,#]` (literal BASE)     ; `sub.w r9,r0,r1` ; ... `udiv r9,r9,rX`
find_tables(img, ref_img, ref_tables) -> same 12 tables located by exact 72-byte contents.
unsafe_refs(img, ranges) -> instructions (decoded at every halfword) that branch to or load a
  literal from inside `ranges` — a conservative stand-in for Ghidra xrefs.
"""
import struct
from capstone import CS_ARCH_ARM, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC

BASE = 0x08004000
CODE_END = 0x080A0000
MHZ = 100_000
cs = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
cs.detail = True


def _dec(img, a, n=6):
    o = a - BASE
    return list(cs.disasm(bytes(img[o:o + 4 * n]), a, n))


def _lit(img, ins):
    """Value loaded by `ldr rX,[pc,#imm]`, else None."""
    if ins.mnemonic not in ("ldr", "ldr.w") or len(ins.operands) != 2:
        return None
    m = ins.operands[1]
    if m.type != ARM_OP_MEM or m.mem.base != ARM_REG_PC:
        return None
    la = ((ins.address + 4) & ~3) + m.mem.disp
    if not (BASE <= la < BASE + len(img) - 3):
        return None
    return struct.unpack_from("<I", img, la - BASE)[0]


def _reg(ins, i):
    return ins.reg_name(ins.operands[i].reg)


def find_sites(img):
    table, dmr = [], []
    end = min(CODE_END, BASE + len(img))
    for a in range(BASE, end - 16, 2):
        ins = _dec(img, a, 6)
        if len(ins) < 4:
            continue
        i0, i1 = ins[0], ins[1]
        base = _lit(img, i0)
        if base is None or base % MHZ or not (4_000 * MHZ // 100 <= base <= 700 * MHZ):
            continue
        rb = _reg(i0, 0)
        if i1.mnemonic == "subs" and len(i1.operands) == 3 and _reg(i1, 2) == rb:
            step = _lit(img, ins[2])
            if step and ins[3].mnemonic == "udiv":
                table.append(dict(site=i1.address, rd=_reg(i1, 0), rs=_reg(i1, 1), rb=rb,
                                  base=base // MHZ, step=step))
        if (i1.mnemonic == "sub.w" and [_reg(i1, k) for k in range(3)] in (["r9", "r0", "r1"], ["sb", "r0", "r1"])
                and rb == "r1" and any(x.mnemonic == "udiv" for x in ins[2:6])):
            dmr.append(dict(site=i1.address, base=base // MHZ))
    return table, dmr


def find_tables(img, ref_img, ref_tables):
    """Tables form one contiguous block, so they all move by one delta. Derive the delta from
    tables whose contents are unique, require every unique one to agree, then require each
    table (incl. the identical RX/TX 220 ones) to have the reference contents at ref+delta."""
    deltas = set()
    for bands in ref_tables.values():
        for addrs in bands.values():
            for ta in addrs:
                row = ref_img[ta - BASE:ta - BASE + 72]
                hits = [i for i in range(0, len(img) - 72, 4) if img[i:i + 72] == row]
                if len(hits) == 1:
                    deltas.add(hits[0] + BASE - ta)
    assert len(deltas) == 1, f"band tables moved inconsistently: {deltas}"
    delta = deltas.pop()
    out = {}
    for kind, bands in ref_tables.items():
        out[kind] = {}
        for band, addrs in bands.items():
            for ta in addrs:
                assert img[ta + delta - BASE:ta + delta - BASE + 72] == ref_img[ta - BASE:ta - BASE + 72], \
                    f"{kind} {band} {ta:#x}: contents differ at +{delta:#x}"
            out[kind][band] = tuple(ta + delta for ta in addrs)
    return out


def unsafe_refs(img, ranges, exclude=()):
    """Branches into / literal loads from any [lo,hi) in ranges, from code outside them."""
    inr = lambda x: any(lo <= x < hi for lo, hi in ranges)
    exc = lambda x: any(lo <= x < hi for lo, hi in exclude) or inr(x)
    bad = []
    for a in range(BASE, min(CODE_END, BASE + len(img)) - 4, 2):
        if exc(a):
            continue
        ins = _dec(img, a, 1)
        if not ins:
            continue
        i = ins[0]
        if i.mnemonic.startswith(("b", "cb")) and i.operands and i.operands[-1].type == ARM_OP_IMM:
            if inr(i.operands[-1].imm):
                bad.append((hex(a), f"{i.mnemonic} {i.op_str}"))
        elif i.mnemonic.startswith("ldr") and len(i.operands) == 2 and i.operands[1].type == ARM_OP_MEM \
                and i.operands[1].mem.base == ARM_REG_PC:
            la = ((a + 4) & ~3) + i.operands[1].mem.disp
            if inr(la) or inr(la + 3):
                bad.append((hex(a), f"{i.mnemonic} {i.op_str} -> lit {la:#x}"))
    # raw data words pointing into ranges (function pointers etc.)
    for o in range(0, len(img) - 3, 4):
        v = struct.unpack_from("<I", img, o)[0] & ~1
        if inr(v) and not exc(o + BASE):
            bad.append((hex(o + BASE), f"data word {v:#x}"))
    return bad
