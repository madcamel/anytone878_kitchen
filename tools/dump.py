#!/usr/bin/env python3
"""dump.py : refresh the grep-able decompile cache work/cache/c/<addr>.c (+ funcs.json).
Only missing files are written; delete work/cache/c to rebuild after big renames."""
import json
from concurrent.futures import ThreadPoolExecutor

import gh
import hcfg

C = hcfg.CACHE / "c"
C.mkdir(parents=True, exist_ok=True)
fs = gh.functions()
(hcfg.CACHE / "funcs.json").write_text(json.dumps(fs))


def one(f):
    p = C / (f["address"] + ".c")
    if p.exists():
        return 0
    try:
        c = gh.decompile(f["address"])
    except gh.ApiError as e:
        c = "// ERROR %s\n" % e
    p.write_text(c)
    return 1


with ThreadPoolExecutor(4) as ex:
    n = sum(ex.map(one, fs))
print("%d functions, %d newly decompiled -> %s" % (len(fs), n, C))
