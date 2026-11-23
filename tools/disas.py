#!/usr/bin/env python3
"""disas.py addr [lo hi] : Ghidra disassembly of the function containing addr (optionally a sub-range).
(Not named dis.py: that shadows the stdlib `dis` module and breaks capstone imports from tools/.)"""
import sys

import gh

a = sys.argv[1]
lo = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0
hi = int(sys.argv[3], 16) if len(sys.argv) > 3 else 1 << 40
for i in gh.disassemble(a):
    if lo <= int(i["address"], 16) <= hi:
        print(i["address"], "%-10s" % i["bytes"], i["text"])
