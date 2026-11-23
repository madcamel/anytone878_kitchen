#!/usr/bin/env python3
"""dec.py N addr... : compact decompile (first N meaningful lines on one line each)."""
import re
import sys

import gh

mx = int(sys.argv[1])
for a in sys.argv[2:]:
    try:
        L = [l.rstrip() for l in gh.decompile(a).splitlines()]
    except gh.ApiError as e:
        print("@@", a, "ERR", e)
        continue
    L = [l for l in L if l.strip() and l.strip() not in "{}" and not re.match(
        r'\s+(undefined\d?|u?int|u?char|byte|bool|short|ushort|code|longlong|ulonglong)\s+\*?[a-zA-Z_]\w*( \[\d+\])?;$', l)]
    print("@@", a, " | ".join(x.strip() for x in L[:mx])[:2500])
    print()
