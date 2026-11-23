#!/usr/bin/env python3
"""sctfields.py : every SCT3258 packet the firmware builds. Finds calls to sct_msg_set_type
(0x08087706), sct_msg_add_field (0x08087718) and sct_msg_read_field (0x08087910) with a constant r0,
groups them by calling function and names fields from the vendor spec if
docs/ref/sct3258_packet_interface.txt is present (scripts/fetch_refs.sh). Output: Txx = TYPE byte,
id:NAME = field added, 'rd id' = field read. -> out/sct_fields.txt"""
import collections
import re

import callsweep
import gh
import hcfg

ADD, TYP, RD = 0x08087718, 0x08087706, 0x08087910
spec = {}
sp = hcfg.ROOT / "docs/ref/sct3258_packet_interface.txt"
if sp.exists():
    for l in sp.read_text(errors="ignore").splitlines():
        m = re.match(r"^([A-Z_0-9]+)\s+0x([0-9a-fA-F]{2,3})\s+[0-9,]", l)
        if m:
            spec.setdefault(int(m.group(2), 16), m.group(1))
byfn = collections.OrderedDict()
for site, t, regs in callsweep.sweep(hcfg.load_fw(), {ADD, TYP, RD}):
    try:
        f = gh.function(site)
        key = (int(f["address"], 16), f["name"])
    except gh.ApiError:
        key = (site, "(no fn) site")
    v = regs["r0"]
    if t == TYP:
        tok = "T??" if v is None else "T%02x" % v
    else:
        tok = ("" if t == ADD else "rd ") + ("??" if v is None else "%02x:%s" % (v, spec.get(v, "?")))
    byfn.setdefault(key, []).append(tok)
lines = ["%08x %-28s %s" % (fa, n, " ".join(v)) for (fa, n), v in sorted(byfn.items())]
hcfg.OUT.mkdir(exist_ok=True)
(hcfg.OUT / "sct_fields.txt").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
