# USB debug channel (RAM peek / poke / call)

A small firmware patch that lets a PC read and write the radio's memory and call firmware
functions over the normal programming cable, **while the radio keeps running normally**. It's the
basis for live experiments: watch UI/RF/DMR state, inject key presses, try register settings on the
RF chip, all without rebuilding firmware for each attempt.

Built by `patches/mkpatch_debug.py` on top of the band-unlock image; verified on a real D878UV.

## How it works

The stock PC-link task (`task_p3_usb_pc_link`) receives every USB packet into `pc_buf`
(0x2000D5A8, length in `pc_rx_len` 0x20001090) and calls `pc_cmd_dispatch`. The patch changes that
single call (the `bl` at 0x08081186 in `usb_pc_link_step`) to call `dbg_hook` instead:

- If the packet is one of the three debug commands below (byte 1 = `'d'`, byte 0 = `D0`/`D1`/`D2`,
  exact length), `dbg_hook` handles it and replies through the firmware's own reply routine.
- Anything else is passed straight to the original `pc_cmd_dispatch`, so the CPS, firmware
  updates and `tools/radio.py dump` behave exactly as stock.

Because it runs *before* the stock session logic, no `PROGRAM` session is opened: the radio doesn't
switch to the "PC MODE" screen and doesn't reboot afterwards. The boot path and the PTT+PF3 update
mode are untouched, so a stock image can always be flashed back.

`dbg_hook` (246 bytes) lives at 0x080065C8 inside two unused ST library functions
(`TIM_ICInit`/`TIM_PWMIConfig`), between literal pools that live neighbouring code still uses; the
builder proves no code or data references the patched range. The image length is unchanged, so the
stock `.CDI`/`.spi` files still apply.

## v2: faster, and alive during transmit

The stock PC-link task only wakes every 10 ms and handles one 64-byte USB packet per wake, which
caps the debug channel at 100 requests/s (≈23 KB/s reading, ≈5 KB/s writing). It also ignores USB
completely while the radio transmits. v2 adds two small hooks (`patches/dbg_fast.c`, in dead ST
library code at 0x080078EC):

- The USB interrupt wakes the PC-link task as soon as a packet arrives, so each request is answered
  right away. USB runs above the priority level uC/OS protects against, so it must not call the OS
  itself: it sets a spare interrupt (IRQ 25) pending, and that handler, at a kernel-safe priority,
  posts the task's flag.
- Debug commands (`D0`/`D1`/`D2`) are served even while transmitting. Everything else (CPS,
  firmware update, NAND dump) still goes through the stock code and its rules.

A request is still one USB packet, so a single poke carries at most 56 bytes.

Measured on a D878UV (v1 → v2): round trip 10 ms → 0.55 ms; reads (240-byte peeks) 23 → 234 KB/s;
writes (56-byte pokes) 4.7 → ~100 KB/s; 30 s of continuous write+verify without an error, radio
running normally (dual watch, APRS RX mod).

## Build and flash

```sh
patches/mkpatch_bandunlock.py uv     # base image (out/D878UV_V4.00_bandunlock_r1/)
patches/mkpatch_debug.py             # -> out/D878UV_V4.00_bandunlock_r1_dbg2/  (sha256 e1d261a9…)
```

The builder refuses anything but the expected input bytes, checks the hook site and the patch
space, assembles the handler with keystone and runs it under unicorn (peek, poke, call, and seven
pass-through cases) before writing.

Flash it: power the radio on holding **PTT + PF3** (red LED blinks), then

```sh
tools/radio.py flash out/D878UV_V4.00_bandunlock_r1_dbg2/D878UV_V4.00_20250725.CDD --yes
```

The radio installs it and restarts by itself. No "Initialize radio" step is needed; settings and
channels are kept. To remove it, flash the stock `firmware/D878UV_V4.00_20250725.CDD` the same way.

## Using it

```sh
tools/radio.py peek 0x20000951 1            # screen_id (0 = standby, 0x0D = menu)
tools/radio.py peek 0x200003c0 4            # current RX frequency, 10 Hz units
tools/radio.py screen                       # screen id, menu node, depth, breadcrumb path
tools/radio.py key 0x0d                     # inject a key (Menu); see codes below
tools/radio.py poke 0x20017f00 12345678 --yes   # write RAM
tools/radio.py call 0x0807F53E 0x20017f00 --yes # call a function (here pc_be32), prints r0/r1
```

Key codes: 0x0D Menu, 0x0E Exit, 0x0F Up, 0x10 Down, 1–10 digits, 0x0B/0x0C probably `*`/`#`,
0x13–0x17 programmable keys. The first key after the radio has idled only wakes the backlight,
exactly like a real press. `key` works by writing the code to `key_post_code` (0x20000901) and
calling `FUN_08028F50`, which posts it to the UI task's mailbox and waits until the UI has handled
it.

From Python, `import radio; r = radio.Radio()` gives `r.peek(addr, n)`, `r.poke(addr, data)`,
`r.call(fn, *args)` and `r.key(code)` (run from `tools/`, or put `tools/` on `sys.path`).

Free scratch RAM for experiments: 0x20015720–0x20018000 (above the initial stack pointer; nothing
uses it). Addresses of interesting state are in `annotations/globals.txt`.

## Wire format

All fields little-endian; the request buffer is word aligned, so offsets 4 and 8 are aligned.

| Command | Request | Reply |
|---|---|---|
| peek | `D0 64 N 00` `addr32` (8 bytes) | `D0 N 00 00` `data[N]` `sum8` (sum of all preceding reply bytes) |
| poke | `D1 64 N 00` `addr32` `data[N]` (8+N bytes) | `D1 06` |
| call | `D2 64 00 00` `fn32` `a0 a1 a2 a3` (24 bytes) | `D2 06 00 00` `r0` `r1` |

- N = 1–255 for peek (the reply buffer is 1 KB; `radio.py` uses ≤240). For poke, keep the request
  inside one 64-byte USB packet (`radio.py` uses ≤48 bytes per request).
- N = 4 on a 4-aligned address, or N = 2 on a 2-aligned address, uses a single word/halfword access,
  which is what peripheral registers need. Anything else is bytewise.
- `call` sets the Thumb bit itself and passes up to four arguments in r0–r3.
- Any other length or byte 1 ≠ `'d'` goes to the stock dispatcher.

## Safety

- The device does **no** address checking. A peek of an unmapped address, a poke in the wrong place
  or a call with bad arguments can hang or reset the radio. Recovery is a power cycle (RAM state is
  lost; flash is untouched). `radio.py` adds host-side guards: peeks only in flash/SRAM/peripheral
  ranges and pokes only in SRAM unless you pass `--any`, and `--yes` on anything that writes or calls.
- Calls run in the USB task (priority 3, above the UI and DMR tasks). Prefer the firmware's own
  locked wrappers (e.g. the semaphore-protected AT1846S register wrapper `FUN_0806199A`) over raw
  drivers, which can collide with another task using the same bus.
- Reads while the radio is transmitting are fine; don't call anything that keys the transmitter
  unless you mean to.
