# AnyTone AT-D878UV firmware harness

Hey folks. This is yet another one of those repos that's designed to be used with an agent like
Claude or Codex. Use a good model. The documentation is obviously LLM written and not really meant
for humans. The human documentation: Plug this into your agent, plug your radio into your PC, and
start hacking!

**Agents:** read `OVERVIEW.md` first and keep it up to date as you work; it has the rules.

Tools for reverse engineering and modifying the firmware of the AnyTone AT-D878UV (and D878UVII)
DMR/analog handhelds, from Linux:

- **A scripted Ghidra project**: one command imports the stock V4.00 firmware and applies all
  shared annotations (~700 function names, ~60 typed RAM globals, memory map, recovered functions),
  so the decompiler reads `if (screen_id == 0xd) menu_open();` instead of `*DAT_08011204 == '\r'`.
- **A headless Ghidra service** with a small HTTP API, and command-line tools on top of it
  (decompile, xrefs, call sweeps, menu maps, renaming) that work well from scripts and LLM agents.
- **Patch builders** that assemble, place and **emulate** firmware patches before writing an image:
  band unlock (region 1) and a USB debug channel (RAM peek/poke/function call/key injection on the
  running radio).
- **`tools/radio.py`**: talks to the radio over the programming cable: flash firmware, dump the
  SPI NAND, and (with the debug patch) inspect and drive it live.

Written knowledge lives in `docs/`: the RAM debug channel (`debug_channel.md`), the PC/USB
protocol (`pc_link.md`), the menu/UI engine (`ui.md`), the band-unlock patch (`patch.md`). `OVERVIEW.md` is a dense map of the whole
firmware (hardware, tasks, RF, DMR chip protocol), written for AI coding agents but useful to humans.

> **The firmware is not included.** It's AnyTone's copyright. Everyone uses their own copy of the
> official V4.00 package; the scripts verify it by hash. Nothing derived from it (Ghidra project,
> builds, decompiles, radio dumps) is committed either; see `.gitignore`.

## Requirements

- Linux, Python 3.10+, `curl`; `pdftotext` (poppler-utils) for the reference docs.
- **Ghidra 12.1.x** (`GHIDRA_INSTALL_DIR`, default `~/opt/ghidra_12.1.4_PUBLIC`) with a JDK it accepts.
- Optional: the GhidraMCP extension (LaurieWired, 2.x) if you want Ghidra as an MCP server for an
  AI assistant (`.mcp.json` is local; the harness itself doesn't need it).
- For radio work: the AnyTone USB programming cable, and access to the serial device (usually
  membership in the `dialout` group).

## Setup

```sh
export GHIDRA_INSTALL_DIR=~/opt/ghidra_12.1.4_PUBLIC
scripts/setup_venv.sh                      # .venv: capstone, keystone, unicorn, pyserial, pyghidra
. .venv/bin/activate
scripts/get_firmware.py ~/Downloads/D878UV*.zip   # -> firmware/, hash-checked
scripts/fetch_refs.sh                      # public datasheets -> docs/ref/ (optional, recommended)
scripts/bootstrap.sh                       # -> work/ghidra, fully annotated (~4 min)
                                           # (or: scripts/project_pack.sh import <shared .gzf>)
scripts/ghidractl start                    # harness API on 127.0.0.1:8081
scripts/selftest.sh                        # should end with ALL PASSED
```

The expected firmware files and hashes are listed in `firmware/README.md` and `tools/hcfg.py`.

## Everyday use

```sh
tools/dec.py 40 0x08044cf2         # compact decompile
tools/decr.py menu_open_addr...    # full decompile
tools/fnprofile.py 3 0x0800d8a8    # what is this function about? (callees, strings, menus)
tools/ramref.py 0x20000951         # who touches screen_id
tools/callsweep.py 0x0807A6FC      # every AT1846S register write with its constant args
echo "0807bb12 adc_dma_init" | tools/ren.py   # rename, then:
tools/names_snapshot.py            # record names in annotations/names.txt
```

To run a Ghidra script on the project, stop the service first (`scripts/ghidractl stop`), run
`$GHIDRA_INSTALL_DIR/support/analyzeHeadless work/ghidra d878 -process D878UV_V4.00.bin -noanalysis
-scriptPath ghidra_scripts -postScript Script.java args...`, then start it again. Using the Ghidra
GUI is fine too: open `work/ghidra/d878.gpr` while the service is stopped.

## Working together

- **`annotations/` is the shared brain**, all plain text so git can merge it: `names.txt`
  (function names), `functions.txt` (exact function boundaries), `globals.txt` (RAM variable
  names/types) and `comments.txt`. Your local Ghidra project is rebuilt from them, so everyone's
  project is identical. After renaming, run `tools/names_snapshot.py` (writes names + functions);
  after adding globals, edit `globals.txt` and re-run `ghidra_scripts/SetupGlobals.java`; export
  comments with `ghidra_scripts/ExportComments.java`.
- **Skipping the bootstrap:** `scripts/project_pack.sh export` writes the analyzed project as a
  `.gzf`; the other person runs `scripts/project_pack.sh import file.gzf`. A `.gzf` contains the
  whole firmware image, so pass it privately to someone who has their own firmware copy, never
  via the repo. Merge work through `annotations/`, not by swapping project files.
- Write findings into `docs/` (human write-ups) and `OVERVIEW.md` (the dense map). Describe
  how things work, not the story of finding them.
- Every patch builder must refuse anything but the exact stock image, assert the bytes it
  replaces, prove its patch space is unreferenced (`patches/bandunlock_sigs.unsafe_refs`) and
  emulate its code under unicorn before writing. Add its pinned output hash to
  `scripts/selftest.sh`.
- Run `scripts/selftest.sh` before pushing.

## Radio safety

- Back up first: `tools/radio.py dump out/nand_backup.bin` reads the whole SPI NAND (codeplug,
  calibration/factory block, contacts) in about 15 minutes. It's read-only. The dump contains your
  personal data: keep it out of the repo (it is, under `out/`).
- Flashing: power on holding **PTT + PF3** (red LED blinks; this is the bootloader, which stays
  intact), then `tools/radio.py flash out/<build>/<stem>.CDD --yes`. A bad image can be fixed the
  same way with the stock `.CDD`. Don't do the PTT+PF1 "Initialize" step unless the radio asks;
  it resets settings and channels.
- Live debugging needs the debug-channel firmware: see `docs/debug_channel.md` (build, flash,
  `radio.py peek/poke/call/key/screen`, wire format).
- `radio.py` refuses writes outside RAM and requires `--yes` for anything that changes the radio.
  The `Y` PC command keys the transmitter; nothing in the harness sends it.
- The band-unlock patch opens TX too. Transmit only where your license allows, into a proper
  antenna or dummy load; the PA and harmonic filters are only designed for the stock bands.

## Layout

```
annotations/     names.txt, globals.txt  (shared RE knowledge)
docs/            debug_channel.md, pc_link.md, ui.md, patch.md, stdperiph_order.txt; docs/ref/ = fetched datasheets (ignored)
ghidra_scripts/  Java scripts: bootstrap pipeline + utilities
patches/         patch builders (+ signature/reference-safety helpers)
scripts/         setup_venv, get_firmware, fetch_refs, bootstrap, ghidractl/ghidra_serve, selftest
tools/           Python CLIs (hcfg.py = config, gh.py = API client, radio.py = radio I/O)
firmware/  work/  out/  vendor/   local only (git-ignored)
```
