#!/usr/bin/env python3
"""decr.py addr... : full decompile. Any leftover DAT_08xxxxxx literal-pool references are rewritten
to the address they hold (g_2000xxxx RAM, io_4xxxxxxx peripheral, c_08xxxxxx flash), so the same
global reads the same everywhere even where annotations/globals.txt has no name for it yet."""
import re
import struct
import sys

import gh
import hcfg

_img = None


def lit(a):
    global _img
    if _img is None:
        _img = hcfg.load_fw()
    o = a - hcfg.BASE
    return struct.unpack_from("<I", _img, o)[0] if 0 <= o <= len(_img) - 4 else None


def resolve(code):
    def r(m):
        v = lit(int(m.group(1), 16))
        if v is None:
            return m.group(0)
        if 0x20000000 <= v < 0x20020000:
            return "g_%08x" % v
        if 0x40000000 <= v < 0x60000000:
            return "io_%08x" % v
        if 0x08000000 <= v < 0x08100000:
            return "c_%08x" % v
        return m.group(0)
    return re.sub(r"DAT_([0-9a-f]{8})", r, code)


def dec(a):
    try:
        return resolve(gh.decompile(a))
    except gh.ApiError as e:
        return "ERR %s %s" % (a, e)


if __name__ == "__main__":
    for a in sys.argv[1:]:
        print(dec(a))
