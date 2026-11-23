"""Tiny client for the harness API served by scripts/ghidra_serve.py (see its docstring)."""
import json
import urllib.error
import urllib.parse
import urllib.request

import hcfg


class ApiError(Exception):
    pass


def _call(req):
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        d = json.load(e)
    except urllib.error.URLError as e:
        raise ApiError("harness API not reachable at %s (scripts/ghidractl start): %s" % (hcfg.API, e.reason))
    if not d.get("success"):
        raise ApiError(d.get("error", "error"))
    return d["data"]


def get(ep, **params):
    return _call(hcfg.API + ep + "?" + urllib.parse.urlencode(params))


def post(ep, **params):
    return _call(urllib.request.Request(hcfg.API + ep, data=urllib.parse.urlencode(params).encode()))


def ahex(a):
    return "0x%x" % a if isinstance(a, int) else (a if a.lower().startswith("0x") else "0x" + a)


def functions():
    out, off = [], 0
    while True:
        d = get("functions", offset=off, limit=2000)
        out += d["items"]
        if not d["has_more"]:
            return out
        off += 2000


def decompile(a):
    return get("decompile", address=ahex(a))["code"]


def function(a):
    return get("function", address=ahex(a))


def xrefs_to(a, limit=500):
    return get("xrefs_to", address=ahex(a), limit=limit)["items"]


def disassemble(a):
    return get("disassemble", address=ahex(a))["items"]


def rename(a, name):
    return post("rename_function", address=ahex(a), new_name=name)


def set_global(a, name, typ="-"):
    return post("set_global", address=ahex(a), name=name, type=typ)


def apply_globals(path=""):
    return post("apply_globals", path=path)


def xrefs_from(a, limit=2000):
    return get("xrefs_from", address=ahex(a), limit=limit)["items"]


_names = None


def resolve(s):
    """Address from hex ('0x0807..', '0807..') or a function / annotations/globals.txt name."""
    global _names
    try:
        return int(s, 16)
    except ValueError:
        pass
    if _names is None:
        _names = {}
        g = hcfg.ROOT / "annotations/globals.txt"
        for line in g.read_text().splitlines():
            f = line.split("#")[0].split()
            if len(f) >= 2:
                _names[f[1]] = int(f[0], 16)
        for f in functions():
            _names.setdefault(f["name"], int(f["address"], 16))
    if s not in _names:
        raise ApiError("unknown name %r" % s)
    return _names[s]
