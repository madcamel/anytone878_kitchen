#!/usr/bin/env python3
"""raminit.py [ramaddr n] : rebuild the initial SRAM image (.data) the firmware's startup code
creates, and write it to out/sram_init.bin (base 0x20000000). Optionally dump n words from ramaddr,
annotated with menu-descriptor names from work/cache/menus.txt.

Keil __scatterload: region table at 0x08004164 ({src,dst,len,fn} entries, offsets relative to the
table); RW data is LZ-compressed (codec __decompress at 0x0800416C), ZI is zeroed. Globals that the
code never writes (RAM-resident const tables such as 0x200006C8) only have values here.
"""
import struct
import sys

import hcfg

d = hcfg.load_fw()
B = hcfg.BASE
u32 = lambda a: struct.unpack_from("<I", d, a - B)[0]  # noqa: E731
T = 0x08004164
lo, hi = (T + u32(T)) & 0xffffffff, (T + u32(T + 4)) & 0xffffffff
R = 0x20000000
ram = bytearray(0x18000)


def decompress(s, o, n):
    end = o + n
    while o < end:
        t = d[s - B]; s += 1
        lit = t & 7 or d[s - B]; s += (t & 7) == 0
        ml = t >> 4 or d[s - B]; s += (t >> 4) == 0
        for _ in range(lit - 1):
            ram[o - R] = d[s - B]; s += 1; o += 1
        if t & 8:
            p = o - d[s - B]; s += 1
            for _ in range(ml + 2):
                ram[o - R] = ram[p - R]; o += 1; p += 1
        else:
            for _ in range(ml):
                ram[o - R] = 0; o += 1


a = lo
while a < hi:
    src, dst, n, fn = struct.unpack_from("<4I", d, a - B)
    a += 16
    if fn & ~1 == 0x0800416C:
        decompress(src, dst, n)
    print("region %08x..%08x from %08x via %08x" % (dst, dst + n, src, fn), file=sys.stderr)
hcfg.OUT.mkdir(exist_ok=True)
(hcfg.OUT / "sram_init.bin").write_bytes(ram)
if len(sys.argv) > 2:
    menus = {}
    mt = hcfg.CACHE / "menus.txt"
    if mt.exists():
        for l in mt.read_text().splitlines():
            menus[int(l.split()[0], 16)] = l.split("|", 1)[1].strip()[:70]
    a = int(sys.argv[1], 16)
    for i in range(int(sys.argv[2], 0)):
        v = struct.unpack_from("<I", ram, a - R + 4 * i)[0]
        print("%08x [%2x] %08x %s" % (a + 4 * i, i, v, menus.get(v, "")))
