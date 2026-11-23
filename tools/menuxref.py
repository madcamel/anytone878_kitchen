#!/usr/bin/env python3
"""menuxref.py : map menu descriptors (work/cache/menus.txt, from tools/menumap.py) to the functions
that reference them -> work/cache/fn_menus.json {func_addr: [[desc, count, type, text], ...]}.
~230 descriptors have no xref (reached through tables); those are simply absent."""
import collections
import json

import gh
import hcfg

fn = collections.defaultdict(list)
for l in (hcfg.CACHE / "menus.txt").read_text().splitlines():
    a, cnt, typ, _, rest = l.split(None, 4)
    for x in gh.xrefs_to(a):
        f = x.get("from_function")
        if f:
            fn[f["address"]].append([a, int(cnt), int(typ), rest.strip()[:60]])
(hcfg.CACHE / "fn_menus.json").write_text(json.dumps(fn))
print("%d functions reference menu descriptors -> %s" % (len(fn), hcfg.CACHE / "fn_menus.json"))
