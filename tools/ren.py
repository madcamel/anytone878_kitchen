#!/usr/bin/env python3
"""ren.py < lines "addr name" : idempotent rename by address. Two passes (via tmp_<addr>) so batches
that permute existing names don't collide. Afterwards run tools/names_snapshot.py to update
annotations/names.txt (the shared record)."""
import sys

import gh

todo = []
for line in sys.stdin:
    line = line.split("#")[0].strip()
    if line:
        a, n = line.split()
        todo.append((a, n))


def current(a):
    try:
        return gh.function(a)["name"]
    except gh.ApiError:
        return None


todo = [(a, n) for a, n in todo if current(a) != n]
bad = 0
for a, _ in todo:
    try:
        gh.rename(a, "tmp_" + a)
    except gh.ApiError as e:
        print("FAIL", a, e); bad += 1
for a, n in todo:
    try:
        gh.rename(a, n)
    except gh.ApiError as e:
        print("FAIL", a, n, e); bad += 1
print(len(todo) - bad, "renamed,", bad, "failed")
