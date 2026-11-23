#!/usr/bin/env python3
"""Talk to the D878UV over its USB CDC programming link (protocol: docs/pc_link.md).

Read-only by default. Subcommands:
  radio.py info                     PROGRAM handshake + model query, then END (radio reboots)
  radio.py dump OUT [START END]     read SPI NAND [START,END) (default all 128 MB) into OUT; rerun with START to resume
  radio.py dump OUT --skip-contacts  same, but skip the DMR ID / digital contact database
                                    (0x04000000-0x044BFFFF index, 0x04500000-0x07FFFFFF records; reald map)
  radio.py read ADDR LEN            hexdump SPI flash
  radio.py flash STEM.CDD --yes     firmware upload; radio must be in update mode (PTT+PF3 at power on)
Debug-patch commands (firmware built by patches/mkpatch_debug.py; work in normal operation, no session):
  radio.py peek ADDR [LEN]          memory read (RAM / flash / peripherals), hexdump
  radio.py poke ADDR HEX --yes      RAM write (0x20000000-0x20018000 only unless --any)
  radio.py call FN [A0..A3] --yes   call a Thumb function, print r0/r1
  radio.py key CODE [CODE..]        inject key codes (0x0D Menu, 0x0E Exit, 0x0F/0x10 Up/Down, 1-10 digits)
  radio.py screen                   print screen ID / menu node / path
"""
import sys, os, time, struct, argparse, fcntl
import serial

import hcfg

PORT = hcfg.PORT
ACK = 0x06


class Radio:
    def __init__(self, port=PORT, timeout=1.0):
        # single user of the port at a time (dump vs. debug sessions)
        hcfg.OUT.mkdir(exist_ok=True)
        self.lock = open(hcfg.OUT / ".radio.lock", "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("radio port busy (out/.radio.lock held)")
        self.lock.write(str(os.getpid())); self.lock.flush()
        for _ in range(30):  # after END the radio reboots; wait for udev perms
            if os.access(port, os.R_OK | os.W_OK): break
            time.sleep(1)
        self.s = serial.Serial(port, 115200, timeout=timeout)
        self.s.reset_input_buffer()
        self.in_session = False

    def xfer(self, data, n=None, wait=0.5):
        self.s.reset_input_buffer()
        self.s.write(data)
        self.s.flush()
        if n is not None:
            return self.s.read(n)
        time.sleep(wait)
        return self.s.read(self.s.in_waiting or 1)

    def program(self):
        r = self.xfer(b"PROGRAM", 3)
        if r != b"QX\x06":
            raise IOError("PROGRAM handshake failed: %r" % r)
        self.in_session = True
        return r

    def model(self):
        r = self.xfer(b"\x02", wait=0.3)
        return r

    def read(self, addr, n=16, tries=3):
        # stock firmware accepts len 1..255 (reply buf 1 KB); 255 gives ~150 KB/s
        assert 1 <= n <= 255 and addr + n <= 0x8000000
        req = b"R" + struct.pack(">IB", addr, n)
        for _ in range(tries):
            r = self.xfer(req, 8 + n)
            if len(r) == 8 + n and r[0] == 0x57 and r[1:6] == req[1:6] and r[-1] == ACK:
                if sum(r[1:6 + n]) & 0xFF == r[6 + n]:
                    return r[6:6 + n]
            time.sleep(0.05)
        raise IOError("read %08x failed: %r" % (addr, r))

    def end(self):
        self.s.write(b"END")
        self.s.flush()
        self.in_session = False

    def close(self):
        self.s.close()

    # ---- debug patch (dbg_hook @0x080065C8) ----
    def peek(self, addr, n):
        out = b""
        while n:
            k = min(n, 240)
            req = bytes([0xD0, 0x64, k, 0]) + struct.pack("<I", addr)
            r = self.xfer(req, 5 + k)
            if len(r) != 5 + k or r[0] != 0xD0 or r[1] != k or sum(r[:4 + k]) & 0xFF != r[4 + k]:
                raise IOError("peek %08x: bad reply %r (debug firmware not running?)" % (addr, r[:16]))
            out += r[4:4 + k]
            addr += k; n -= k
        return out

    def poke(self, addr, data, any_addr=False):
        if not any_addr and not (0x20000000 <= addr and addr + len(data) <= 0x20018000):
            raise ValueError("poke outside SRAM refused (use --any)")
        for i in range(0, len(data), 48):  # stay inside one 64-byte USB packet
            c = data[i:i + 48]
            r = self.xfer(bytes([0xD1, 0x64, len(c), 0]) + struct.pack("<I", addr + i) + c, 2)
            if r != b"\xd1\x06":
                raise IOError("poke %08x: bad reply %r" % (addr + i, r))

    def call(self, fn, *args):
        a = list(args) + [0] * (4 - len(args))
        r = self.xfer(bytes([0xD2, 0x64, 0, 0]) + struct.pack("<5I", fn, *a), 12, )
        if len(r) != 12 or r[:2] != b"\xd2\x06":
            raise IOError("call %08x: bad reply %r" % (fn, r))
        return struct.unpack_from("<II", r, 4)

    def key(self, code):
        """Same as keypad_scan: key code -> 0x20000901, FUN_08028F50 posts it to the UI task's mailbox
        and blocks until task_p10_keys_ui has handled it (so the reply means the key is done)."""
        self.poke(0x20000901, bytes([code]))
        self.call(0x08028F50)

    # ---- firmware update (protocol: pc_write_codeplug_block, screen 0x13) ----
    def flash(self, cdd_path, log=print):
        cdd = open(cdd_path, "rb").read()
        stem = cdd_path[:-4]
        cdi, spi = open(stem + ".CDI", "rb").read(), open(stem + ".spi", "rb").read()
        model, hw = spi[14:20], spi[22:26]
        start, size = struct.unpack_from("<II", cdi, 256)
        assert len(spi) == 51 and len(cdi) == 278 and cdi[:6] == model, "unexpected .spi/.CDI"
        assert size == len(cdd) == struct.unpack_from("<I", spi, 6)[0], "size mismatch"
        assert start == 0x08004000, "load address %#x" % start
        r = self.xfer(b"UPDATE", 1)
        if r != b"\x06":
            raise IOError("UPDATE not acked (%r): is the radio in update mode (PTT+PF3)?" % r)
        ident = self.xfer(b"\x02", wait=0.3)
        if ident[1:7] != model or ident[9:13] != hw:
            raise IOError("radio %r does not match firmware %r/%r" % (ident, model, hw))
        log("radio %s %s: sending %d bytes" % (model.decode(), hw.decode(), size))
        data = cdd + bytes(-len(cdd) % 32)
        t0 = time.time()
        for off in range(0, len(data), 32):
            blk = struct.pack("<I", start + off) + data[off:off + 32]
            pkt = b"\x01" + blk + struct.pack("<H", sum(blk) & 0xFFFF) + b"\x06"
            for _ in range(3):
                if self.xfer(pkt, 1) == b"\x06":
                    break
            else:
                raise IOError("block %#x not acked; stop here and retry the whole upload" % (start + off))
            if off % 0x8000 == 0:
                log("  %6.1f%%  %.0f s" % (100.0 * off / len(data), time.time() - t0))
        if self.xfer(b"\x18", 1) != b"\x06":
            raise IOError("close (0x18) not acked")
        log("upload complete (%.0f s)" % (time.time() - t0))


def hexdump(addr, b):
    for i in range(0, len(b), 16):
        c = b[i:i + 16]
        print("%08x  %-48s %s" % (addr + i, " ".join("%02x" % x for x in c),
                                  "".join(chr(x) if 32 <= x < 127 else "." for x in c)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--no-end", action="store_true", help="leave session open (no reboot)")
    ap.add_argument("--yes", action="store_true", help="confirm a write/flash/call")
    ap.add_argument("--skip-contacts", action="store_true", help="dump: skip the DMR ID database")
    ap.add_argument("--any", action="store_true", help="allow addresses outside the safe ranges")
    a = ap.parse_args()
    r = Radio(a.port)
    try:
        if a.cmd == "info":
            print("PROGRAM ->", r.program())
            m = r.model()
            print("model ->", m, m.hex())
        elif a.cmd == "read":
            r.program()
            addr, n = int(a.args[0], 0), int(a.args[1], 0)
            buf = b""
            for x in range(addr, addr + n, 16):
                buf += r.read(x, min(16, addr + n - x))
            hexdump(addr, buf)
        elif a.cmd == "dump":
            out = a.args[0]
            start = int(a.args[1], 0) if len(a.args) > 1 else 0
            stop = int(a.args[2], 0) if len(a.args) > 2 else 0x8000000  # 1 Gbit SPI NAND
            r.program()
            mode = "r+b" if os.path.exists(out) else "wb"
            with open(out, mode) as f:
                t0 = time.time()
                C = 240  # 15x16: keeps progress/alignment arithmetic simple
                skip = [(0x04000000, 0x044C0000), (0x04500000, 0x08000000)] if a.skip_contacts else []
                for x in range(start, stop, C):
                    if any(lo <= x < hi for lo, hi in skip):
                        continue
                    f.seek(x)  # file offset == flash address, so START resumes in place
                    f.write(r.read(x, min(C, stop - x)))
                    if (x - start) % (C * 4096) == 0:
                        f.flush()
                        el = time.time() - t0
                        done = x - start + 16
                        print("%08x  %.0f KB/s  eta %.0f s" % (x, done / 1024 / max(el, 1e-3),
                              (stop - x) / max(done / max(el, 1e-3), 1)), flush=True)
        elif a.cmd == "flash":
            if not a.yes:
                ap.error("flash rewrites the radio firmware; pass --yes")
            r.flash(a.args[0])
        elif a.cmd == "peek":
            addr = int(a.args[0], 0); n = int(a.args[1], 0) if len(a.args) > 1 else 64
            if not (0x08000000 <= addr < 0x08100000 or 0x20000000 <= addr < 0x20018000
                    or 0x40000000 <= addr < 0x50000000 or 0xE0000000 <= addr < 0xE0100000) and not a.any:
                ap.error("address outside flash/SRAM/peripherals (use --any)")
            hexdump(addr, r.peek(addr, n))
        elif a.cmd == "poke":
            if not a.yes:
                ap.error("poke writes live RAM; pass --yes")
            r.poke(int(a.args[0], 0), bytes.fromhex(a.args[1]), a.any)
            print("ok")
        elif a.cmd == "call":
            if not a.yes:
                ap.error("call runs arbitrary firmware code; pass --yes")
            r0, r1 = r.call(*[int(x, 0) for x in a.args])
            print("r0=%#010x r1=%#010x" % (r0, r1))
        elif a.cmd == "key":
            for k in a.args:
                r.key(int(k, 0)); time.sleep(0.3)
            print("screen=%#04x" % r.peek(0x20000951, 1)[0])
        elif a.cmd == "screen":
            scr = r.peek(0x20000951, 1)[0]; node = r.peek(0x20000B20, 1)[0]; depth = r.peek(0x20000B30, 1)[0]
            path = struct.unpack("<10I", r.peek(0x20007DF0, 40))
            print("screen=%#04x node=%d depth=%d path=%s" % (scr, node, depth, list(path[:max(depth, 1)])))
        else:
            ap.error("unknown cmd")
    finally:
        if r.in_session and not a.no_end:
            r.end()
        r.close()


if __name__ == "__main__":
    main()
