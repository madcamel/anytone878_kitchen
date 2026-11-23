"""Minimal static linker for one clang-compiled Thumb-2 object (ELF32 ARM, REL relocations).

link(obj_bytes, base, externs) -> (blob, symbols)
  Places every SHF_ALLOC section (.text*, .rodata*) back to back from `base` (keeping alignment),
  resolves relocations and returns the bytes plus {name: address} of defined global and local
  symbols (functions with the Thumb bit set).
  externs: {name: (address, is_function)} for undefined symbols (firmware functions/globals and
  builder-provided symbols such as MOD_RAM).

Writable data (.data/.bss/COMMON) is refused: mod state lives in the mod's RAM slice, addressed
through MOD_RAM. Supported relocations are the ones clang emits for -mcpu=cortex-m4 -Os code:
ABS32, THM_CALL, THM_JUMP24, THM_MOVW_ABS_NC, THM_MOVT_ABS. Anything else is an error, so a new
code pattern fails the build instead of producing a wrong image.
"""
import struct

SHT_SYMTAB, SHT_NOBITS, SHT_REL, SHT_RELA = 2, 8, 9, 4
SHF_WRITE, SHF_ALLOC = 1, 2
STT_FUNC, STT_SECTION = 2, 3
SHN_UNDEF, SHN_ABS, SHN_COMMON = 0, 0xFFF1, 0xFFF2
R_ARM_ABS32, R_ARM_THM_CALL, R_ARM_THM_JUMP24 = 2, 10, 30
R_ARM_THM_MOVW_ABS_NC, R_ARM_THM_MOVT_ABS = 47, 48
R_ARM_V4BX, R_ARM_NONE = 40, 0


class LinkError(Exception):
    pass


def _sections(o):
    if o[:4] != b"\x7fELF" or o[4] != 1 or o[5] != 1:
        raise LinkError("not a little-endian ELF32 object")
    shoff, = struct.unpack_from("<I", o, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", o, 0x2E)
    secs = []
    for i in range(shnum):
        name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from("<10I", o, shoff + i * shentsize)
        secs.append(dict(i=i, name_off=name, type=typ, flags=flags, off=off, size=size, link=link,
                         info=info, align=max(align, 1), entsize=entsize))
    strtab = secs[shstrndx]
    for s in secs:
        s["name"] = _cstr(o, strtab["off"] + s["name_off"])
    return secs


def _cstr(o, off):
    return o[off:o.index(b"\0", off)].decode()


def _symbols(o, secs):
    st = next((s for s in secs if s["type"] == SHT_SYMTAB), None)
    if st is None:
        return []
    strs = secs[st["link"]]
    out = []
    for k in range(st["size"] // 16):
        name, value, size, info, other, shndx = struct.unpack_from("<IIIBBH", o, st["off"] + 16 * k)
        out.append(dict(name=_cstr(o, strs["off"] + name) if name else "", value=value,
                        type=info & 15, bind=info >> 4, shndx=shndx))
    return out


def _thm_bl_get(ins):
    h1, h2 = struct.unpack("<HH", ins)
    s = (h1 >> 10) & 1
    j1, j2 = (h2 >> 13) & 1, (h2 >> 11) & 1
    i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
    v = (s << 24) | (i1 << 23) | (i2 << 22) | ((h1 & 0x3FF) << 12) | ((h2 & 0x7FF) << 1)
    return v - (1 << 25) if s else v


def _thm_bl_put(ins, off):
    if not -(1 << 24) <= off < (1 << 24) or off & 1:
        raise LinkError("Thumb branch offset %#x out of range" % off)
    v = off & 0x1FFFFFF
    s = (v >> 24) & 1
    i1, i2 = (v >> 23) & 1, (v >> 22) & 1
    j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
    h1, h2 = struct.unpack("<HH", ins)
    h1 = (h1 & 0xF800) | (s << 10) | ((v >> 12) & 0x3FF)
    h2 = (h2 & 0xD000) | (j1 << 13) | (j2 << 11) | ((v >> 1) & 0x7FF)
    return struct.pack("<HH", h1, h2)


def _movw_get(ins):
    h1, h2 = struct.unpack("<HH", ins)
    return ((h1 & 0xF) << 12) | (((h1 >> 10) & 1) << 11) | (((h2 >> 12) & 7) << 8) | (h2 & 0xFF)


def _movw_put(ins, imm):
    h1, h2 = struct.unpack("<HH", ins)
    h1 = (h1 & 0xFBF0) | ((imm >> 12) & 0xF) | (((imm >> 11) & 1) << 10)
    h2 = (h2 & 0x8F00) | (((imm >> 8) & 7) << 12) | (imm & 0xFF)
    return struct.pack("<HH", h1, h2)


def link(o, base, externs):
    secs = _sections(o)
    syms = _symbols(o, secs)
    for s in secs:
        if s["flags"] & SHF_ALLOC and (s["flags"] & SHF_WRITE or s["type"] == SHT_NOBITS) and s["size"]:
            raise LinkError("writable section %s (%d bytes): keep mod state in MOD_RAM" % (s["name"], s["size"]))
        if s["type"] == SHT_RELA:
            raise LinkError("RELA relocations not supported")
    if any(x["shndx"] == SHN_COMMON for x in syms):
        raise LinkError("COMMON symbol: compile with -fno-common and no writable globals")
    # place sections (EHABI unwind tables are dropped: nothing unwinds through mod code)
    addr, place, blob = base, {}, bytearray()
    for s in secs:
        if s["flags"] & SHF_ALLOC and s["size"] and not s["name"].startswith((".ARM.exidx", ".ARM.extab")):
            pad = -addr % s["align"]
            blob += b"\0" * pad
            addr += pad
            place[s["i"]] = addr
            blob += o[s["off"]:s["off"] + s["size"]]
            addr += s["size"]

    def sym_addr(x):
        if x["shndx"] == SHN_UNDEF:
            if x["name"] not in externs:
                raise LinkError("undefined symbol %r (not in annotations/ or builder symbols)" % x["name"])
            a, is_fn = externs[x["name"]]
            return a | 1 if is_fn else a, is_fn
        if x["shndx"] == SHN_ABS:
            return x["value"], False
        if x["shndx"] not in place:
            raise LinkError("symbol %r in non-allocated section" % x["name"])
        a = place[x["shndx"]] + x["value"]
        return a, x["type"] == STT_FUNC          # clang already sets bit 0 on Thumb STT_FUNC values

    for rs in secs:
        if rs["type"] != SHT_REL or rs["info"] not in place:
            continue
        tgt = rs["info"]
        tbase = place[tgt]
        for k in range(rs["size"] // 8):
            r_off, r_info = struct.unpack_from("<II", o, rs["off"] + 8 * k)
            typ, si = r_info & 0xFF, r_info >> 8
            if typ in (R_ARM_NONE, R_ARM_V4BX):
                continue
            S, is_fn = sym_addr(syms[si])
            P = tbase + r_off
            lo = P - base
            ins = bytes(blob[lo:lo + 4])
            if typ == R_ARM_ABS32:
                A, = struct.unpack("<I", ins)
                blob[lo:lo + 4] = struct.pack("<I", (S + A) & 0xFFFFFFFF)
            elif typ in (R_ARM_THM_CALL, R_ARM_THM_JUMP24):
                A = _thm_bl_get(ins)
                blob[lo:lo + 4] = _thm_bl_put(ins, (S & ~1) + A - P)   # A = -4 for "bl ."
            elif typ == R_ARM_THM_MOVW_ABS_NC:
                A = _movw_get(ins)
                A = A - 0x10000 if A & 0x8000 else A
                blob[lo:lo + 4] = _movw_put(ins, (S + A) & 0xFFFF)
            elif typ == R_ARM_THM_MOVT_ABS:
                A = _movw_get(ins)
                A = A - 0x10000 if A & 0x8000 else A
                blob[lo:lo + 4] = _movw_put(ins, ((S + A) >> 16) & 0xFFFF)
            else:
                raise LinkError("unsupported relocation type %d at %#x (symbol %r)" % (typ, P, syms[si]["name"]))
    out = {}
    for x in syms:
        if x["name"] and not x["name"].startswith("$") and x["shndx"] in place and x["type"] != STT_SECTION:
            out[x["name"]] = sym_addr(x)[0]
    return bytes(blob), out
