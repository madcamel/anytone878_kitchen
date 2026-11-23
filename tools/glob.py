#!/usr/bin/env python3
"""glob.py : name/type RAM globals live (no service restart) and record them in annotations/globals.txt.

  glob.py ADDR NAME [TYPE] [comment words...]   one global
  glob.py < lines                               batch, same format as globals.txt ("addr name type # c")
  glob.py --apply                               re-apply the whole globals.txt to the running project

TYPE: u8 u16 u32 ptr, T[N], or - (label only; default). An existing line for ADDR is replaced; a
comment is kept unless a new one is given. New addresses go under "# --- unsorted" at the end;
move them into a section when convenient. The file stays the source of truth: a fresh bootstrap
(SetupGlobals.java) produces the same result."""
import re
import sys

import gh
import hcfg

PATH = hcfg.ROOT / "annotations/globals.txt"
SECTION = "# --- unsorted (tools/glob.py)"


def fmt(a, name, typ, comment):
    s = "%08X %-22s %-6s" % (a, name, typ)
    return (s + " # " + comment if comment else s).rstrip()


def parse(line):
    body, _, comment = line.partition("#")
    f = body.split()
    if len(f) < 2:
        return None
    return int(f[0], 16), f[1], (f[2] if len(f) > 2 else "-"), comment.strip()


def upsert(lines, a, name, typ, comment):
    for i, line in enumerate(lines):
        p = None if line.lstrip().startswith("#") else parse(line)
        if p and p[0] == a:
            lines[i] = fmt(a, name, typ, comment or p[3])
            return "updated"
    if SECTION not in lines:
        lines.append(SECTION)
    lines.append(fmt(a, name, typ, comment))
    return "added"


def main():
    argv = sys.argv[1:]
    if argv == ["--apply"]:
        r = gh.apply_globals(str(PATH))
        print("applied %d globals%s" % (r["applied"], "; removed stale labels: " + " ".join(r["removed"]) if r["removed"] else ""))
        return
    if argv:
        if len(argv) < 2:
            sys.exit(__doc__)
        if len(argv) > 2 and re.fullmatch(r"(u8|u16|u32|ptr)(\[\d+\])?|-", argv[2]):
            typ, rest = argv[2], argv[3:]
        else:
            typ, rest = "-", argv[2:]
        todo = [(int(argv[0], 16), argv[1], typ, " ".join(rest))]
    else:
        todo = [p for p in map(parse, sys.stdin) if p]
    lines = PATH.read_text().splitlines()
    used = {p[1]: p[0] for p in (parse(l) for l in lines if not l.lstrip().startswith("#")) if p}
    bad = 0
    for a, name, typ, comment in todo:
        if used.get(name, a) != a:
            print("FAIL %08X %s: name already used at %08X" % (a, name, used[name]))
            bad += 1
            continue
        try:
            r = gh.set_global(a, name, typ)
        except gh.ApiError as e:
            print("FAIL %08X %s: %s" % (a, name, e))
            bad += 1
            continue
        how = upsert(lines, a, name, typ, comment)
        used[name] = a
        print("%s %08X %s %s%s" % (how, a, name, typ, "  (dropped label %s)" % ",".join(r["removed"]) if r["removed"] else ""))
    PATH.write_text("\n".join(lines) + "\n")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
