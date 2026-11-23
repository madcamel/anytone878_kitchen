#!/usr/bin/env python3
"""Serve the analyzed Ghidra project headlessly. Run via scripts/ghidractl, not directly.

Harness API (all tools use this), default http://127.0.0.1:8081/ :
  GET  /program_info
  GET  /functions?offset=&limit=           {items:[{name,address}], total, has_more}
  GET  /function?address=                  function containing address
  GET  /function_bodies?offset=&limit=     {items:[{name,address,ranges:[[start,end],...]}]}
  GET  /xrefs_to?address=&limit=           {items:[{from_address,type,from_function}]}
  GET  /xrefs_from?address=&limit=
  GET  /decompile?address=                 {name,address,code}  (uses the program's decompiler options)
  GET  /disassemble?address=               {items:[{address,bytes,text}]}
  POST /rename_function  address=&new_name=
  POST /set_global  address=&name=[&type=]   label + data type (u8 u16 u32 ptr, T[N], - = label only);
                                           other labels at that address are removed (globals.txt is truth)
  POST /apply_globals [path=]              re-apply every line of annotations/globals.txt, live
  xrefs_from items also carry to_symbol (primary symbol at the target, or null).
Responses are {"success": true, "data": ...} or {"success": false, "error": "..."}.

Optional: if the GhidraMCP extension (LaurieWired, 2.x, with HeadlessBackend) is installed in
Ghidra, its backend is also started on --mcp-port (8080) for the `ghidra` MCP server in .mcp.json.
Nothing in the harness depends on it. Note its decompiler ignores program options, so it shows
RAM globals as *PTR_name_08xxxxxx; the harness /decompile shows them by name.

Saves every SAVE_EVERY seconds when changed, and on SIGTERM/SIGINT. Ghidra project APIs stay on
the main thread (the one that started the JVM); HTTP threads only read/edit the program.
"""
import argparse
import fcntl
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GHIDRA = Path(os.environ.get("GHIDRA_INSTALL_DIR", Path.home() / "opt/ghidra_12.1.4_PUBLIC"))
SAVE_EVERY = 120
DECOMPILERS = 4

ap = argparse.ArgumentParser()
ap.add_argument("--project-dir", default=str(ROOT / "work/ghidra"))
ap.add_argument("--project-name", default="d878")
ap.add_argument("--program", default="/D878UV_V4.00.bin")
ap.add_argument("--port", type=int, default=8081)
ap.add_argument("--mcp-port", type=int, default=8080)
ap.add_argument("--lock", default=str(ROOT / "work/ghidra/serve.flock"))
args = ap.parse_args()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ghidra_serve")

# Single instance: a second start exits instead of fighting over the project lock.
lockf = open(args.lock, "w")
try:
    fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    log.error("another ghidra_serve instance holds %s", args.lock)
    sys.exit(1)

stop = False


def on_signal(signum, _frame):
    global stop
    log.info("signal %d: stopping", signum)
    stop = True


signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

os.environ["GHIDRA_INSTALL_DIR"] = str(GHIDRA)
import pyghidra  # noqa: E402

pyghidra.start(install_dir=GHIDRA, verbose=False)


# Once the JVM runs, a crashed main thread leaves non-daemon Java threads (GhidraMCP backend)
# alive and nothing to handle SIGTERM: ghidractl stop then hangs. Exit hard instead.
def _die(t, v, tb):
    log.critical("startup failed", exc_info=(t, v, tb))
    logging.shutdown()
    os._exit(1)


sys.excepthook = _die
import jpype  # noqa: E402

mcp_jar = GHIDRA / "Ghidra/Extensions/GhidraMCP/lib/GhidraMCP.jar"
if mcp_jar.exists():
    jpype.addClassPath(str(mcp_jar))
from ghidra.util.task import TaskMonitor  # noqa: E402
from ghidra.app.decompiler import DecompInterface, DecompileOptions  # noqa: E402
from ghidra.program.model.symbol import SourceType, SymbolType  # noqa: E402
from ghidra.program.model.data import (ArrayDataType, ByteDataType, PointerDataType,  # noqa: E402
                                       UnsignedIntegerDataType, UnsignedShortDataType)

# The JVM installs its own SIGTERM/SIGINT handlers at startup, which exit the process without
# running the save/close below. Re-install ours now that the JVM is up.
signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

project = pyghidra.open_project(str(Path(args.project_dir).resolve()), args.project_name, create=False)
program, consumer = pyghidra.consume_program(project, args.program)
fm = program.getFunctionManager()
refm = program.getReferenceManager()
listing = program.getListing()
af = program.getAddressFactory()
wlock = threading.Lock()          # program writes + saves

mcp = None
if mcp_jar.exists():
    try:
        mcp = jpype.JClass("com.lauriewired.ghidramcp.headless.HeadlessBackend").start(program, args.mcp_port)
        log.info("GhidraMCP backend on http://127.0.0.1:%d/", mcp.getPort())
    except Exception as e:  # optional
        log.warning("GhidraMCP backend not started: %s", e)

decomp_pool = queue.Queue()
for _ in range(DECOMPILERS):
    di = DecompInterface()
    opts = DecompileOptions()
    opts.grabFromProgram(program)     # respect read-only memory etc.
    di.setOptions(opts)
    di.openProgram(program)
    decomp_pool.put(di)


def addr(s):
    a = af.getAddress(s if s.lower().startswith("0x") else "0x" + s)
    if a is None:
        raise ValueError("bad address %r" % s)
    return a


def fdict(f):
    return None if f is None else {"name": str(f.getName()), "address": str(f.getEntryPoint())}


def ep_program_info(q):
    return {"name": str(program.getName()), "language": str(program.getLanguageID()),
            "functions": fm.getFunctionCount(),
            "blocks": [{"name": str(b.getName()), "start": str(b.getStart()), "end": str(b.getEnd()),
                        "perm": ("r" if b.isRead() else "-") + ("w" if b.isWrite() else "-")
                        + ("x" if b.isExecute() else "-")}
                       for b in program.getMemory().getBlocks()]}


def ep_functions(q):
    off, lim = int(q.get("offset", 0)), int(q.get("limit", 500))
    allf = [fdict(f) for f in fm.getFunctions(True)]
    return {"items": allf[off:off + lim], "total": len(allf), "offset": off, "has_more": off + lim < len(allf)}


def ranges(f):
    return [[str(r.getMinAddress()), str(r.getMaxAddress())] for r in f.getBody().getAddressRanges()]


def ep_function_bodies(q):
    off, lim = int(q.get("offset", 0)), int(q.get("limit", 500))
    allf = list(fm.getFunctions(True))
    items = [dict(fdict(f), ranges=ranges(f)) for f in allf[off:off + lim]]
    return {"items": items, "total": len(allf), "offset": off, "has_more": off + lim < len(allf)}


def ep_function(q):
    f = fm.getFunctionContaining(addr(q["address"]))
    if f is None:
        raise ValueError("no function at " + q["address"])
    d = fdict(f)
    d.update(body_start=str(f.getBody().getMinAddress()), body_end=str(f.getBody().getMaxAddress()),
             body_size=int(f.getBody().getNumAddresses()), signature=str(f.getSignature()))
    return d


def ep_xrefs_to(q):
    lim = int(q.get("limit", 500))
    out = []
    for r in refm.getReferencesTo(addr(q["address"])):
        fa = r.getFromAddress()
        out.append({"from_address": str(fa), "type": str(r.getReferenceType()),
                    "from_function": fdict(fm.getFunctionContaining(fa))})
        if len(out) >= lim:
            break
    return {"items": out}


def ep_xrefs_from(q):
    lim = int(q.get("limit", 500))
    a = addr(q["address"])
    f = fm.getFunctionContaining(a)
    srcs = f.getBody().getAddresses(True) if f else [a]
    out = []
    for s in srcs:
        for r in refm.getReferencesFrom(s):
            ta = r.getToAddress()
            sym = program.getSymbolTable().getPrimarySymbol(ta)
            out.append({"from_address": str(s), "to_address": str(ta), "type": str(r.getReferenceType()),
                        "to_function": fdict(fm.getFunctionAt(ta)),
                        "to_symbol": str(sym.getName()) if sym is not None else None})
            if len(out) >= lim:
                return {"items": out}
    return {"items": out}


def ep_decompile(q):
    f = fm.getFunctionContaining(addr(q["address"]))
    if f is None:
        raise ValueError("no function at " + q["address"])
    di = decomp_pool.get()
    try:
        r = di.decompileFunction(f, 60, TaskMonitor.DUMMY)
    finally:
        decomp_pool.put(di)
    if r is None or not r.decompileCompleted():
        raise ValueError("decompile failed: %s" % (r.getErrorMessage() if r else "no result"))
    return {"name": str(f.getName()), "address": str(f.getEntryPoint()),
            "code": str(r.getDecompiledFunction().getC())}


def ep_disassemble(q):
    f = fm.getFunctionContaining(addr(q["address"]))
    if f is None:
        raise ValueError("no function at " + q["address"])
    out = []
    for ins in listing.getInstructions(f.getBody(), True):
        out.append({"address": str(ins.getAddress()),
                    "bytes": bytes(b & 0xff for b in ins.getBytes()).hex(), "text": str(ins)})
    return {"items": out}


def ep_rename_function(q):
    a = addr(q["address"])
    f = fm.getFunctionAt(a)
    if f is None:
        raise ValueError("no function starts at " + q["address"])
    with wlock:
        tx = program.startTransaction("rename %s" % a)
        ok = False
        try:
            old = str(f.getName())
            f.setName(q["new_name"], SourceType.USER_DEFINED)
            ok = True
        finally:
            program.endTransaction(tx, ok)
    return {"address": str(a), "old_name": old, "new_name": q["new_name"]}


BASE_TYPES = {"u8": ByteDataType(), "u16": UnsignedShortDataType(),
              "u32": UnsignedIntegerDataType(), "ptr": PointerDataType()}


def parse_type(t):
    """Same grammar as ghidra_scripts/SetupGlobals.java parseType."""
    if t in ("", "-"):
        return None
    n = 0
    if "[" in t:
        n = int(t[t.index("[") + 1:t.index("]")])
        t = t[:t.index("[")]
    if t not in BASE_TYPES:
        raise ValueError("type %r (u8 u16 u32 ptr, T[N], -)" % t)
    base = BASE_TYPES[t]
    return ArrayDataType(base, n, base.getLength()) if n > 0 else base


def set_global(a, name, t):
    """Caller holds a transaction. Make `name` the only label at a; apply type."""
    st = program.getSymbolTable()
    dt = parse_type(t)
    keep = None
    for s in list(st.getSymbols(a)):
        if str(s.getName()) == name:
            keep = s
    if keep is None:
        keep = st.createLabel(a, name, SourceType.USER_DEFINED)
    keep.setPrimary()
    removed = []
    for s in list(st.getSymbols(a)):
        if s != keep and s.getSymbolType() == SymbolType.LABEL:
            removed.append(str(s.getName()))
            s.delete()
    if dt is not None:
        listing.clearCodeUnits(a, a.add(dt.getLength() - 1), False)
        listing.createData(a, dt)
    return removed


def write_tx(what, fn):
    with wlock:
        tx = program.startTransaction(what)
        ok = False
        try:
            r = fn()
            ok = True
            return r
        finally:
            program.endTransaction(tx, ok)


def ep_set_global(q):
    a = addr(q["address"])
    removed = write_tx("set_global %s" % a, lambda: set_global(a, q["name"], q.get("type", "-")))
    return {"address": str(a), "name": q["name"], "type": q.get("type", "-"), "removed": removed}


def ep_apply_globals(q):
    path = Path(q.get("path") or ROOT / "annotations/globals.txt")
    lines = []
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            f = line.split()
            lines.append((addr(f[0]), f[1], f[2] if len(f) > 2 else "-"))

    def run():
        removed = []
        for a, n, t in lines:
            removed += ["%s:%s" % (a, x) for x in set_global(a, n, t)]
        return removed
    removed = write_tx("apply_globals", run)
    return {"applied": len(lines), "removed": removed}


GET = {"/program_info": ep_program_info, "/functions": ep_functions, "/function": ep_function,
       "/function_bodies": ep_function_bodies,
       "/xrefs_to": ep_xrefs_to, "/xrefs_from": ep_xrefs_from, "/decompile": ep_decompile,
       "/disassemble": ep_disassemble}
POST = {"/rename_function": ep_rename_function, "/set_global": ep_set_global,
        "/apply_globals": ep_apply_globals}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _run(self, table, q):
        u = urllib.parse.urlparse(self.path)
        try:
            fn = table.get(u.path)
            if fn is None:
                raise KeyError("unknown endpoint %s" % u.path)
            body, code = {"success": True, "data": fn(q)}, 200
        except KeyError as e:
            body, code = {"success": False, "error": str(e)}, 404
        except Exception as e:
            body, code = {"success": False, "error": "%s: %s" % (type(e).__name__, e)}, 400
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
        self._run(GET, q)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        self._run(POST, q)


srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
log.info("serving %s: harness API on http://127.0.0.1:%d/", program.getName(), args.port)


def save(why):
    with wlock:
        if program.isChanged():
            program.save("ghidra_serve: %s" % why, TaskMonitor.DUMMY)
            log.info("saved (%s)", why)


last = time.monotonic()
try:
    while not stop:
        time.sleep(0.5)
        if time.monotonic() - last >= SAVE_EVERY:
            last = time.monotonic()
            try:
                save("periodic")
            except Exception:
                log.exception("periodic save failed")
finally:
    srv.shutdown()
    if mcp is not None:
        mcp.stop()
    while not decomp_pool.empty():
        decomp_pool.get().dispose()
    try:
        save("shutdown")
    finally:
        program.release(consumer)
        project.close()
        log.info("closed")
