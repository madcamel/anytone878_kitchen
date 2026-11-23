#!/usr/bin/env python3
"""trace.py [-d DEPTH] [-n LINES] [-q] TARGET... : one-shot map of a function.

Prints, for each TARGET (hex address or function name):
  callers        functions that call it
  callees        named ones, then unnamed (FUN_*) ones
  globals        RAM/peripheral addresses touched by it, with name (or g_xxxxxxxx) and access kinds
  decompile      the function itself, then every unnamed callee down to DEPTH (default 1),
                 each capped at LINES lines (default 60; the target itself at 4×LINES); literal-pool DAT_ resolved as in decr.py
-q skips the decompiles (summary only). Name what you learn with ren.py / glob.py."""
import collections
import sys

import gh
from decr import resolve as resolve_dat
from xrefs import kind


def is_unnamed(n):
    return n.startswith(("FUN_", "thunk_FUN_"))


def calls_from(items):
    return {x["to_function"]["address"]: x["to_function"]["name"] for x in items
            if ("CALL" in x["type"] or "JUMP" in x["type"]) and x.get("to_function")}


def data_refs(items):
    out = collections.defaultdict(collections.Counter)
    for x in items:
        try:
            ta = int(x["to_address"], 16)
        except ValueError:                  # stack/register refs, e.g. "Stack[-0x1c]"
            continue
        if 0x20000000 <= ta < 0x20020000 or 0x40000000 <= ta < 0x60000000:
            out[(ta, x.get("to_symbol"))][kind(x["type"])] += 1
    return out


def decomp(a, cap):
    try:
        L = resolve_dat(gh.decompile(a)).splitlines()
    except gh.ApiError as e:
        return ["  ERR %s" % e]
    L = [l for l in L if l.strip()]
    return L[:cap] + (["  ... (%d more lines)" % (len(L) - cap)] if len(L) > cap else [])


def trace(t, depth, cap, quiet):
    a = gh.resolve(t)
    f = gh.function(a)
    fa = f["address"]
    items = gh.xrefs_from(fa)
    callees = calls_from(items)
    callers = sorted({x["from_function"]["name"] for x in gh.xrefs_to(fa)
                      if x.get("from_function") and ("CALL" in x["type"] or "JUMP" in x["type"])})
    print("#### %s %s  (%d bytes)" % (f["name"], fa, f["body_size"]))
    print("callers:", ", ".join(callers) or "-")
    named = sorted(n for n in callees.values() if not is_unnamed(n))
    unnamed = sorted(callees.items())
    print("callees:", ", ".join(named) or "-")
    print("unnamed:", ", ".join(n for _, n in unnamed if is_unnamed(n)) or "-")
    g = data_refs(items)
    if g:
        print("globals:")
        for (ta, sym), ks in sorted(g.items()):
            nm = sym if sym and not sym.startswith("DAT_") else "g_%08x" % ta
            print("  %08x %-28s %s" % (ta, nm, " ".join(k + ("x%d" % n if n > 1 else "") for k, n in sorted(ks.items()))))
    if quiet:
        return
    print()
    print("\n".join(decomp(fa, cap * 4)))
    seen = {fa}
    frontier = [ca for ca, n in unnamed if is_unnamed(n)]
    for _ in range(depth):
        nxt = []
        for ca in frontier:
            if ca in seen:
                continue
            seen.add(ca)
            print("\n---- callee %s" % ca)
            print("\n".join(decomp(ca, cap)))
            nxt += [c for c, n in calls_from(gh.xrefs_from(ca)).items() if is_unnamed(n)]
        frontier = nxt


def main():
    args = sys.argv[1:]
    depth, cap, quiet = 1, 60, False
    while args and args[0].startswith("-"):
        o = args.pop(0)
        if o == "-d":
            depth = int(args.pop(0))
        elif o == "-n":
            cap = int(args.pop(0))
        elif o == "-q":
            quiet = True
        else:
            sys.exit(__doc__)
    if not args:
        sys.exit(__doc__)
    for t in args:
        trace(t, depth, cap, quiet)
        print()


if __name__ == "__main__":
    main()
