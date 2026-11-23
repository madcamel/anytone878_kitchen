#!/usr/bin/env python3
"""Find AnyTone menu descriptor tables in the D878UV image.
Layout: char *items[count] (EN) ; char *items[count] (CN) ; then
struct {char **items; u8 count; u8 type; u16 0} desc[2] = {EN, CN}.
Writes work/cache/menus.txt ("desc_addr count type | EN items"); tools/menuxref.py then maps
descriptors to the functions that use them. Optional arg: a different image to scan (stdout)."""
import struct, sys
import hcfg
B = hcfg.BASE
d = open(sys.argv[1], "rb").read() if len(sys.argv) > 1 else hcfg.load_fw()
E = B + len(d)
u32 = lambda a: struct.unpack_from("<I", d, a - B)[0]
def cstr(a):
    o = a - B; e = d.find(b"\0", o, o + 64)
    return None if e < 0 else d[o:e]
def is_str(a):
    if not (B <= a < E): return False
    s = cstr(a)
    return s is not None and all(c >= 0x20 or c in (9, 10) for c in s)
def desc(a):
    if a + 8 > E: return None
    p, cnt, typ, z = u32(a), d[a-B+4], d[a-B+5], d[a-B+6:a-B+8]
    if not (B <= p < E and p % 4 == 0 and 1 <= cnt <= 64 and z == b"\0\0"): return None
    if p + 4*cnt > E: return None
    ptrs = [u32(p + 4*i) for i in range(cnt)]
    if not all(is_str(x) for x in ptrs): return None
    return p, cnt, typ, ptrs
out = []
for a in range(B, E - 16, 4):
    x, y = desc(a), desc(a + 8)
    if x and y and x[1] == y[1] and x[0] != y[0]:
        en = [cstr(q).decode("latin1") for q in x[3]]
        if not any(any(ord(c) > 0x7e for c in s) for s in en):  # EN entry first
            out.append((a, x[1], x[2], en))
lines = ["%08x %2d %2d | %s" % (a, cnt, typ, " / ".join(en)) for a, cnt, typ, en in out]
if len(sys.argv) > 1:
    print("\n".join(lines))
else:
    hcfg.CACHE.mkdir(parents=True, exist_ok=True)
    (hcfg.CACHE / "menus.txt").write_text("\n".join(lines) + "\n")
print("# %d menus" % len(out), file=sys.stderr)
