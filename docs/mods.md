# Mods

A **mod** is a self-contained piece of new firmware code: its own C source, its own RAM, its own
hooks into the stock firmware, and its own emulation test. Mods are built on top of the **baseline**
(band-unlock r1 + the USB debug channel), which every patched image has.

```sh
patches/modbuild.py --list               # available mods
patches/modbuild.py capture              # -> out/D878UV_V4.00_base+capture/  (.CDD/.CDI/.spi/.bin, mods.map)
patches/modbuild.py A B                  # several mods in one image (if their hooks don't overlap)
tools/radio.py flash out/D878UV_V4.00_base+capture/D878UV_V4.00_20250725.CDD --yes
tools/radio.py mods                      # what a running radio has installed
```

Needs `clang` (the system clang 14 works; it only compiles, the harness links).

## Where mods live

| | Address | Notes |
|---|---|---|
| Mod area header | flash 0x080E7800 | `D878MODS`, format, count, then one 32-byte entry per mod (name, version, code and RAM ranges) |
| Mod code | flash 0x080E7C00 … 0x080F57FF | ~55 KB; past the end of the stock image, never touched by the firmware |
| Mod RAM | 0x20015720 … 0x20017FFF | ~10 KB above the initial stack pointer; each mod gets its own slice |

Patched images are therefore longer than stock (the area is padded with 0xFF, which also wipes
whatever older firmware left there). The bootloader accepts that: it writes any address it's sent and
doesn't check the image at boot. Flashing a stock image afterwards leaves the old mod code in that
area, but nothing calls it.

## Writing a mod

```
patches/mods/<name>/
  mod.py      NAME, VERSION, RAM (bytes), SOURCES (one .c file), HOOKS, test()
  <name>.c    the code; #include "fw.h"
```

- **Firmware access by name.** `patches/mods/fw.h` declares firmware functions and globals; the
  builder resolves each name from `annotations/names.txt` / `globals.txt`. To use something new,
  name it in Ghidra (`tools/ren.py`, `tools/glob.py`), then declare it in `fw.h`.
- **State** goes in the mod's RAM slice: `MOD_RAM` … `MOD_RAM_END`, typically
  `#define S ((volatile struct state *)MOD_RAM)`. No writable C globals (the build refuses them).
  The slice isn't cleared at boot; initialise it yourself.
- **Hooks** are byte patches at a stock address, written as Thumb assembly where `{symbol}` means a
  symbol of this mod:
  `dict(site=0x08004A4C, orig="032801d106f092f8", asm="nop\nnop\nbl {cap_tim6}", why="…")`.
  The builder checks the original bytes, checks the site isn't touched by the baseline or another
  selected mod, and requires the replacement to be exactly as long as the original.
- **`test(e, m, fw)`** runs the built image under unicorn (`patches/modemu.py`): stub firmware
  functions, enter at the hook site, check the mod's behaviour. A mod without a meaningful test
  shouldn't be flashed.
- Interrupt code stays integer-only.

## Independence

Mods don't call each other or share RAM. If two mods need the same hook site they are
**either/or**: the builder refuses to combine them.

| Mod | What | Notes |
|---|---|---|
| `aprs_rx` | analog APRS receive on the MCU (`docs/aprs.md`) | either/or with `capture` (TIM6 dispatch) |
| `capture` | squelch-triggered RX audio recorder for APRS development (`tools/aprscap.py`) | either/or with `aprs_rx` |
| `scope` | on-radio spectrum waterfall of the current band (VHF / mid / UHF); open with key function 55 "Predicting" on any PF/P key, knob = cursor, P2 = zoom in (finest level tunes the VFO there; from channel mode it switches to VFO first), P1 = zoom out / exit, Exit exits, PTT exits and transmits. Without a tune the radio is left exactly as it was (`patches/mods/scope/hwtest.py` diffs it on hardware) | combines with either; hooks p10 key/PTT calls and p12 `ui_render` |
