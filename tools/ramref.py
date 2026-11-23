#!/usr/bin/env python3
"""ramref.py [-r N] addr... : functions referencing a RAM/peripheral address.
Plain: Ghidra xrefs (literal-pool pointers are typed by ghidra_ghidra_scripts/SetupGlobals.java).
-r N : also count literal pools holding addr-N..addr, i.e. code that reaches addr as a field of a
       struct whose base is up to N bytes below it (shown as name[+off])."""
import collections
import struct
import sys

import gh
import hcfg

args = sys.argv[1:]
R = 0
if args and args[0] == "-r":
    R = int(args[1], 0)
    args = args[2:]
img = hcfg.load_fw() if R else None
for s in args:
    t = int(s, 0)
    hits = collections.Counter()
    targets = [(t, 0)]
    if R:
        seen = set()
        for o in range(0, len(img) - 3, 4):
            v = struct.unpack_from("<I", img, o)[0]
            if t - R <= v < t and v not in seen:
                seen.add(v)
                targets.append((v, t - v))
    for a, off in targets:
        for x in gh.xrefs_to(a):
            f = x.get("from_function")
            if f:
                hits[(f["name"], off)] += 1
    print("%08x:" % t, " ".join(n + ("[+%d]" % d if d else "") for (n, d) in sorted(hits)))
