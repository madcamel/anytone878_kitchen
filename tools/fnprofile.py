#!/usr/bin/env python3
"""fnprofile.py depth addr... : what a function is about. Walks callees to `depth`, then lists named
callees (Ghidra call references), string labels and menu descriptors touched (needs work/cache/menus.txt + fn_menus.json from
tools/menumap.py and tools/menuxref.py). Fastest way to guess an unnamed function's role."""
import collections
import json
import re
import sys

import gh
import hcfg

fnm = {}
p = hcfg.CACHE / "fn_menus.json"
if p.exists():
    fnm = json.loads(p.read_text())
SKIP_PREFIX = ("OS_CPU_SR",)


def dec(a):
    try:
        return gh.decompile(a)
    except gh.ApiError:
        return ""


def callees(a):
    try:
        return {x["to_function"]["address"]: x["to_function"]["name"] for x in gh.get("xrefs_from", address="0x" + a, limit=2000)["items"]
                if ("CALL" in x["type"] or "JUMP" in x["type"]) and x.get("to_function")}  # jumps to an entry = tail call
    except gh.ApiError:
        return {}


depth = int(sys.argv[1])
for root in sys.argv[2:]:
    root = root.lower().replace("0x", "").zfill(8)
    seen = {root}; frontier = [root]; named = collections.Counter(); ms = []; strs = collections.Counter()
    for _ in range(depth):
        nxt = []
        for a in frontier:
            ms += [m[3][:50] for m in fnm.get(a, [])]
            for t in re.findall(r'\bs_([A-Za-z0-9_]{3,40}?)_[0-9a-f]{8}', dec(a)):
                strs[t] += 1
            for ca, name in callees(a).items():
                if not name.startswith(("FUN_", "thunk_")) and not name.startswith(SKIP_PREFIX):
                    named[name] += 1
                if ca not in seen:
                    seen.add(ca); nxt.append(ca)
        frontier = nxt
    print("== %s: %d funcs reached" % (root, len(seen)))
    print("  named:", ", ".join(k for k, _ in named.most_common(40)))
    if strs:
        print("  strings:", ", ".join(k for k, _ in strs.most_common(30)))
    if ms:
        print("  menus:", " || ".join(sorted(set(ms))[:25]))
