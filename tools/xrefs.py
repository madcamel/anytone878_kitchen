#!/usr/bin/env python3
"""xrefs.py [-w|-r|-c] [-v] TARGET... : who references each target, grouped by function.

TARGET is a hex address or a function / globals.txt name. Each function is listed once with its
access kinds: W write, R read, C call, J jump, D data (literal-pool pointer), P param (address
passed). -w / -r / -c keep only functions that write / read / call. -v lists the sites as well.
For struct fields reached through a base pointer use ramref.py -r."""
import collections
import re
import sys

import gh

KIND = {"WRITE": "W", "READ": "R", "DATA": "D", "PARAM": "P", "READ_WRITE": "RW"}


def kind(t):
    if "CALL" in t:
        return "C"
    if "JUMP" in t:
        return "J"
    return KIND.get(t, t[:1])


def main():
    args = sys.argv[1:]
    only = verbose = None
    while args and args[0].startswith("-"):
        o = args.pop(0)
        if o == "-v":
            verbose = True
        elif o in ("-w", "-r", "-c"):
            only = o[1].upper()
        else:
            sys.exit(__doc__)
    if not args:
        sys.exit(__doc__)
    for t in args:
        a = gh.resolve(t)
        per = collections.OrderedDict()
        for x in gh.xrefs_to(a, limit=5000):
            f = x.get("from_function")
            key = (f["name"], f["address"]) if f else ("<no function>", "")
            per.setdefault(key, []).append((x["from_address"], kind(x["type"])))
        label = "" if re.fullmatch(r"(0x)?[0-9a-fA-F]+", t) else t
        print("== %08x %s  (%d refs, %d functions)" % (a, label, sum(map(len, per.values())), len(per)))
        for (name, fa), sites in sorted(per.items()):
            kinds = collections.Counter(k for _, k in sites)
            if only and not any(only in k for k in kinds):
                continue
            ks = " ".join(k + ("x%d" % n if n > 1 else "") for k, n in sorted(kinds.items()))
            line = "  %-34s %-9s %s" % (name, fa, ks)
            if verbose:
                line += "   @ " + " ".join("%s:%s" % (s, k) for s, k in sites)
            print(line)


if __name__ == "__main__":
    main()
