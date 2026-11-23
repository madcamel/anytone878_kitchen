#!/usr/bin/env python3
"""showrange.py lo hi [N] : compact view of every cached decompile in [lo,hi) (needs tools/dump.py)."""
import json
import re
import sys

import hcfg

lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
mx = int(sys.argv[3]) if len(sys.argv) > 3 else 25
for f in json.loads((hcfg.CACHE / "funcs.json").read_text()):
    if lo <= int(f["address"], 16) < hi:
        L = [l.rstrip() for l in open(hcfg.CACHE / "c" / (f["address"] + ".c"))]
        L = [l for l in L if l.strip() and l.strip() not in "{}" and not re.match(
            r'\s+(undefined\d?|u?int|u?char|byte|bool|short|ushort|code|longlong|ulonglong)\s+\*?[a-zA-Z_]\w*;$', l)]
        print("@@", f["address"], f["name"], " | ".join(x.strip() for x in L[:mx])[:1400])
