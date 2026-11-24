# D878UV V4.00 — UI / menu system

No library: this is hand-rolled vendor code, built on the well-known Chinese-embedded
"KeyTab" menu pattern (a table of `{index, up, down, enter, back, fn}` nodes driven by a
current-index variable), reorganised so that **each table group is one menu depth level** and
the actual menu content comes from data descriptors plus a breadcrumb path. Addresses are the
D878UV V4.00 image; all names below are applied in the Ghidra project.

## 1. Layers

```
keypad_scan (p14, 10 ms) ──mbox──> task_p10_keys_ui ──> ptt_press/ptt_release (0x18/0x19)
                                                    └─> ui_key_handler  (key × screen matrix)
                                                              │ screen==0x0D (menu)
                                                              ▼
                                   menu_key_updown / menu_key_enter / menu_key_back
                                                              │ node = KeyTab[node].{up,down,enter,back}
                                                              ▼
                                           menu_L<n>_{show,up,down,enter,back}  (vtable rows)
                                                              │ app_event_post(dirty bits)
                                                              ▼
                       task_p12_display ──> ui_render ──switch(screen)──> paint_menu / paint_standby / ...
```

## 2. Global UI state

| Address | Meaning |
|---|---|
| 0x20000951 | **screen ID** (see §6). Everything in `ui_key_handler` and `ui_render` switches on it |
| 0x200001B0 | current key code (§7) |
| 0x20007FF8 | radio settings block (`+5` language 0 EN / 1 CN, `+0x10..0x14` programmable-key functions, `+0x2C`, `+0x4E` …) |
| 0x200003DC | pointer to the current channel record (the menus edit this one) |
| 0x2000095C / 0x20000958 | pending / active display dirty bits (`app_event_post` ORs into 0x2000095C and wakes p12; p12 copies to 0x20000958) |
| 0x2000094C / 0x2000094E | current foreground / background colour (RGB565) |

## 3. The menu engine (screen 0x0D)

**KeyTab** at 0x080AE958: 77 × 12-byte nodes `{u8 self, up, down, ev3, ev4, enter, back, pad; void (*fn)(void)}`.
Nodes come in 11 groups of 7: group *k* (head node 7k) = menu depth level *k*+1.
Only the head carries links (to its own 6 children), so a group is effectively a **vtable**:

| slot | name | role |
|---|---|---|
| 0 | `menu_L<n>_show` | build + draw the list for this level from the breadcrumb path |
| 1 / 2 | `_up` / `_down` | move the cursor (shared list logic; `L1_up/down` contain it) |
| 3 / 4 | `_ev3_nop` / `_ev4_nop` | every one is `bx lr`: unused events (probably left/right in the original template) |
| 5 | `_enter` | descend: `menu_goto_node(7(n), 1)` (or act, for leaf items) |
| 6 | `_back` | ascend; at L1 → `menu_exit_to_standby` |

* Current node 0x20000B20; current fn 0x20000B78; "needs redraw" flag 0x20000AF4.
* `menu_key_updown/enter/back` do `node = KeyTab[node].field; KeyTab[node].fn(); menu_redraw_if_dirty()`.
  Event handlers finish with `menu_goto_node(path_node[depth-2], 0)`, returning the node to the
  level's head; `menu_redraw_if_dirty` re-runs the head when AF4 is set.
* `menu_open` (Menu key from standby) resets everything and calls `menu_goto_node(0, 1)`.

**Breadcrumb stack** (pushed by `menu_path_push` when a level is shown for the first time,
depth 0x20000B30, max 20). Per level: `path` 0x20007DF0[] (selected logical item — the
important one), prev cursor 0x20007E40[], window top 0x20007EF4[], 0x20007F44[], node
0x20007FE4[], item-string ptr 0x20007E90[], widget type 0x20007EE0[], count 0x20007F94[].

**Level heads are giant nested switches over the path.** `menu_L2_show` does
`switch(path[0])`, `menu_L3_show` `switch(path[0]) → switch(path[1])`, and so on to L11.
That's why L3–L6 are the largest functions in the image (7–15 KB). Every case does the same
thing: load the list widget (`count` 0x20000AFC, `items` 0x20000B24, `type` 0x20000B00)
from a **menu descriptor**, and set the title 0x20000B2C (parent item's string).

**Menu descriptor** (706 of them, `work/cache/menus.txt`; `tools/menumap.py`):
`char *en[n]; char *cn[n]; struct {char **items; u8 count; u8 type; u16 0} desc[2]`,
always read as `desc[settings.lang]`. Code references the desc only, never the strings.

**Hiding items**: there's no per-item visibility flag. Code shrinks `count` (hidden items must
be last) or remaps visible→logical index with a per-menu function (`menu_top_vis_to_id`,
`menu_settings_vis_to_id`, `chanset_vis_to_field`), and the *logical* index is what's
pushed on the path.

**Shared list widget**: cursor 0x20000AD4, previous cursor 0x20000AD8, window top 0x20000B04
(4 visible rows; scrolling keeps the cursor in the window), layout 0x20000ADC..0x20000AEC.
Up/down post `0x20000 | 4 | 0x20<<row(old) | 0x20<<row(new)` (only the two changed rows are
repainted) or `0x1E1` (whole list). `paint_menu` 0x0803A3D0 draws title bar (y 0–0x11),
4-row list (y 0x12–0x69), softkey bar (y 0x6A–0x7F).

**Widget type** (descriptor `type` / 0x20000B00):

| type | meaning |
|---|---|
| 1 | plain list (most menus, option lists, also some 1-line notices) |
| 2 | dynamic list built in code (zones, channels, contacts) |
| 7, 8, 0x14 | confirm boxes; on Enter, `menu_popup_unwind` re-runs heads to pop back 2 levels |
| 9 | alert popup (two lines) |
| 0x0A | info page, Up/Down page by 2 lines (Device Info) |
| 0x0B | numeric entry (Input ID, Chan Num) |
| 0x0C | text / frequency entry (Name, Input Rx Freq) |
| 0x10 | Yes/No confirmation ("Confirm To Save?") |
| 0x11 | progress / sending ("Sending Message ...") |
| 0x13 | wrap-around selector |
| 0x0D–0x0F, 0x16–0x18 | in-place editors (Up/Down ignored by the list code) |

## 4. Top level (path[0]) and Settings

Top menu: desc 0x080A2430 (analog channel) / 0x080A23C0 (digital channel):
`0 Book|Talk Group, 1 SMS, 2 Call Log, 3 Zone, 4 Scan, 5 Roaming, 6 Settings, 7 Record,
8 GPS, 9 Digi Moni, 10 BT, 11 APRS`. `menu_top_hide_count` drops items for analog
channels, units with no BT (`FUN_08072BCC`), and `FUN_080704A2`.

Settings (path[0]=6): `Radio Set / Chan Set / Device Info` (0x080A5C78; the variant without
Chan Set is 0x080A5C98).
Radio Set → `Voice Func / Display Func / Key Func / Other Func` (0x080A5CC8).

## 5. Chan Set (path 6 → 1 → item): a sub-engine of its own

There are four item lists, one per channel type (byte +8 &3): analog 0x080A9360,
digital 0x080A9278, A+D 0x080A9468, D+A 0x080A9570.
`chanset_vis_to_field(row)` maps the row to a **canonical field ID**, and:

* `chanset_build_options(row)` (L4) loads the option list for that field from the RAM table
  0x200006C8[field] and **preselects the cursor from the channel record** (this is the bitfield
  map below).
* `chanset_commit(row, value, extra)` 0x08018AB6 writes the value back into the record. Most
  fields then call `rf_cmd_post(1)` (retune).

0x200006C8 is initialised `.data`, not code-written. Get the values from `tools/raminit.py`,
which decompresses Keil's `__scatterload` RW image.

| field | name | options desc | channel record bits |
|---|---|---|---|
| 0 | New Chan | — | — |
| 1 | Delete Chan | — | — |
| 2 | Channel Type | 0x080A95A0 A / D / A+D TX A / D+A TX D | +8 [1:0] |
| 3 | TCDT | 0x080A95C8 Off / CTC / DCS | +9 [3:2] (+0x0A CTCSS idx, +0x0C DCS) |
| 4 / 5 | RCDT / RTCDT | 0x080A95C8 | +9 [1:0] |
| 6 | Optional Signal | 0x080A9628 Off / DTMF / 2TONE / 5TONE | +0x1A [7:4] |
| 7 | Tx Power | 0x080A95F8 Lo / Mi / Hi / Turbo | +8 [3:2] |
| 8 | Offset | — | +0x39 |
| 9 | Squelch mode | 0x080A9660 SQ / CDT / TONE / C&T / C\|T | +0x19 [7:4] |
| **0x0A** | **Band width** | **0x080A9680 Narrow / Wide** | **+8 [5:4]** |
| 0x0B | Reverse | 0x080A96A0 | +9 [4] |
| 0x0C / 0x0D | Rx / Tx Freq | text entry | — |
| 0x0E | TalkAround | 0x080A96E8 | +9 [7] |
| 0x10 / 0x11 | Busy Lock / Tx Allow | 0x080A9710 / 0x080A9740 | +0x1A [3:0] |
| 0x12 | TX Prohibit | 0x080A9760 | +9 [6:5] |
| 0x1A–0x1C | RX / TX / R&T CC | 0x080A9860 | +0x20, +0x43 |
| 0x1D | Time Slot | 0x080A9880 | +0x21 [0] |
| 0x1E / 0x1F | Digi Encrypt / Encrypt Type | 0x080A99E8 / 0x080A9A08 | +0x3A, +0x21 [6] |
| 0x21–0x28 | Work Alone, CH Ranging, APRS Rx, SMS/DataAck Forbid, DMR Mode, Slot Suit … | | +0x21 / +0x34 / +0x3B bits |

## 6. Screen IDs (0x20000951)

| ID | screen | set by / painted by |
|---|---|---|
| 0 | standby (main) | many; `paint_standby` 0x0803F830 (also for 1–6, 9) |
| 1 / 2 | keypad entry from standby (2 = direct freq/channel entry) | `standby_digit_key` |
| 3 | sending DTMF | p6 sequencer |
| 4 / 5 / 6 | 2-tone / 5-tone / DTMF decode display | 0x0800A442 / 0x0800BCA8 / 0x0800BF28 |
| 7 / 8 | call alerts (Menu → 0x08044E98 / 0x08044EDC) | `task_p11_event_handler`; `paint_alert` |
| 9 | DMR call in progress | 0x08068BC4 |
| 0x0A, 0x0B | unidentified (0x0B shows the channel's CC/slot) | 0x08012542 / 0x080662A6; `paint_alert` |
| 0x0C | unidentified (Up/Down → 0x08079594), probably GPS/roaming | 0x08076CA4; `paint_screen_0c` |
| **0x0D** | **menu** | `menu_L1_show`; `paint_menu` |
| 0x0E / 0x0F | quick numeric-input / quick list pickers (bg 0x85BE) | 0x08059F7C / 0x08059FBA; `paint_menu` |
| 0x10 | "Booting… Please Wait" | |
| 0x11 | power-on logo / text | |
| 0x12 | "WARNING This is Boot Mode for Sct" | |
| 0x14 | BT UART↔USB passthrough | |
| 0x1E / 0x1F | "Program Error" / "Flash Error" | |
| 0x20, 0x21, 0x23–0x26 | Record USB, PC RECORD, PC MODE / READ / WRITE, "Copying Data" | |
| 0x27 / 0x28 | TEST MODE / TEST STANDARD / TEST BHT (factory) | |

## 7. Keys (0x200001B0)

The key codes below come from how `ui_key_handler` uses them:

| code | key |
|---|---|
| 1–10 | digits; 10 is probably "0" |
| 0x0B / 0x0C | probably `*` / `#` |
| 0x0D | Menu / OK (confirmed on hardware; the first key after idle only wakes the backlight) |
| 0x0E | Exit / Back |
| 0x0F / 0x10 | Up / Down (confirmed on hardware) |
| 0x11 / 0x12 | channel knob (rotary encoder): `EXTI9_5_IRQHandler` (PC8 edge, compared with PC9) sets `encoder_pending_key11/12`, `encoder_poll` (p14) posts the key; channel/zone step in standby |
| 0x13 / 0x14 | **P1 / P2** (front). Function = settings +0x13 / +0x14, long press +0x44 / +0x45 |
| 0x15 / 0x16 / 0x17 | **PF1 / PF2 / PF3** (side). Function = settings +0x10 / +0x11 / +0x12, long press +0x41 / +0x42 / +0x43 |
| 0x18 / 0x19 | PTT press / release |
| 0x1B, 0x1C, 0x1D | long-press variants |

`ui_key_handler` is one ~1100-line function: a stack of global gates (mode locks, key lock,
call states) followed by `switch(key)`, each case an if-chain over the screen ID.

## 7a. Programmable-key functions (`key_fn_table` 0x200001B4)

The five programmable keys (PF1–PF3, P1, P2; key codes above) each store a **function ID** per
press type: short at settings +0x10…+0x14, long at +0x41…+0x45 (codeplug 0x02500010 / 0x02500041;
long-press time +0x46). The ID is the index into the radio's own key-function list (menu descriptor
0x080A6758, 57 labels; the CPS writes the same numbers) and into **`key_fn_table`**, a 112-entry
function-pointer table in **RAM** (initialised `.data`, so it's visible only in `out/sram_init.bin`
from `tools/raminit.py`, not as a flash table).

Dispatch (`ui_key_handler`, outside menus/pickers 0x0D–0x0F; `pf_key_function_id` maps key → ID):
- short press (`key_press_state` 1, `key_long_press` 0): `key_fn_table[id]()`
- long press (`key_press_state` 2 / `key_long_press` set): `key_fn_table[long_id]()`
- **momentary IDs** (`pf_function_is_momentary`: 9 Sub PTT, 12 Alarm, 18 Monitor, 46 TBST Send):
  press → `key_fn_table[id]`, **release** (`key_press_state` 0) → `key_fn_table[57 + id]`
- entries 57–111 that aren't release handlers, and ID 55 "Predicting", are `key_pf_nop` (it only
  clears 0x20000066, the "key handled" state).

Because the table is in RAM, a mod can add or replace a key function by storing one pointer; no code
hook is needed for the dispatch itself. ID 55 (Predicting) is the natural slot to take over: it's in
the radio's menu, does nothing in V4.00, and has no release handler.

| ID | menu label | handler | release handler [57+ID] | notes |
|---|---|---|---|---|
| 0 (0x00) | Off | `key_pf_off` |  |  |
| 1 (0x01) | Volt | `key_pf_volt` |  |  |
| 2 (0x02) | Tx Power | `key_pf_tx_power` |  |  |
| 3 (0x03) | TalkAround | `key_pf_talkaround` |  |  |
| 4 (0x04) | Reverse | `key_pf_reverse` |  |  |
| 5 (0x05) | Digi Encrypt | `key_pf_digi_encrypt` |  |  |
| 6 (0x06) | Call | `key_pf_call` |  |  |
| 7 (0x07) | VOX | `key_pf_vox` |  |  |
| 8 (0x08) | VFO/MR | `key_pf_vfo_mr` |  |  |
| 9 (0x09) | Sub Ptt | `key_pf_sub_ptt` | `key_pf_sub_ptt_release` | momentary |
| 10 (0x0A) | Scan | `key_pf_scan` |  |  |
| 11 (0x0B) | FM Radio | `key_pf_fm_radio` |  |  |
| 12 (0x0C) | Alarm | `key_pf_alarm` | `key_pf_nop` | momentary |
| 13 (0x0D) | Record Switch | `key_pf_record_switch` |  |  |
| 14 (0x0E) | Record | `key_local_record` |  |  |
| 15 (0x0F) | SMS | `key_pf_sms` |  |  |
| 16 (0x10) | Dial | `key_pf_dial` |  |  |
| 17 (0x11) | GPS Info | `key_pf_gps_info` |  | not in reald's CPS list |
| 18 (0x12) | Monitor | `key_pf_monitor` | `key_pf_monitor_release` | momentary |
| 19 (0x13) | Main CH Switch | `key_pf_main_ch_switch` |  |  |
| 20 (0x14) | Hot Key 1 | `key_pf_hot_key` |  |  |
| 21 (0x15) | Hot Key 2 | `key_pf_hot_key` |  |  |
| 22 (0x16) | Hot Key 3 | `key_pf_hot_key` |  |  |
| 23 (0x17) | Hot Key 4 | `key_pf_hot_key` |  |  |
| 24 (0x18) | Hot Key 5 | `key_pf_hot_key` |  |  |
| 25 (0x19) | Hot Key 6 | `key_pf_hot_key` |  |  |
| 26 (0x1A) | Work Alone | `key_pf_work_alone` |  |  |
| 27 (0x1B) | Nuisance delete | `key_pf_nuisance_delete` |  |  |
| 28 (0x1C) | Digi Monitor | `key_pf_digi_monitor` |  |  |
| 29 (0x1D) | Sub Ch Hide | `key_sub_ch_toggle` |  |  |
| 30 (0x1E) | Prior Zone | `key_pf_prior_zone` |  |  |
| 31 (0x1F) | Program Scan | `key_pf_program_scan` |  |  |
| 32 (0x20) | Enhance Sound | `key_pf_enhance_sound` |  |  |
| 33 (0x21) | LastCall Reply | `key_pf_lastcall_reply` |  |  |
| 34 (0x22) | Switch ChType | `key_pf_switch_chtype` |  |  |
| 35 (0x23) | Ranging | `key_pf_ranging` |  | not in reald's CPS list |
| 36 (0x24) | Roaming | `key_pf_roaming` |  |  |
| 37 (0x25) | CH Ranging | `key_pf_ch_ranging` |  | not in reald's CPS list |
| 38 (0x26) | Max VOL Set | `key_pf_max_vol_set` |  |  |
| 39 (0x27) | Slot Set | `key_pf_slot_set` |  |  |
| 40 (0x28) | Aprs Type | `key_pf_aprs_type` |  | not in reald's CPS list |
| 41 (0x29) | Zone Select | `key_pf_zone_select` |  |  |
| 42 (0x2A) | Roaming Set | `key_pf_roaming_set` |  |  |
| 43 (0x2B) | APRS Set | `key_pf_aprs_set` |  | not in reald's CPS list |
| 44 (0x2C) | FixTime Mute | `key_pf_fixtime_mute` |  |  |
| 45 (0x2D) | CTC/DCS Set | `key_pf_ctc_dcs_set` |  |  |
| 46 (0x2E) | TBST Send | `key_pf_tbst_send` | `key_pf_tbst_send_release` | momentary |
| 47 (0x2F) | BHT On/Off | `key_bt_toggle` |  |  |
| 48 (0x30) | Gps On/Off | `key_pf_gps_on_off` |  |  |
| 49 (0x31) | Ch.Name | `key_pf_ch_name` |  |  |
| 50 (0x32) | CDT Scan | `key_pf_cdt_scan` |  |  |
| 51 (0x33) | Aprs Send | `key_pf_aprs_send` |  | radio menu only (newer than reald's CPS list) |
| 52 (0x34) | Ana Aprs Info | `key_aprs_info_open` |  | radio menu only (newer than reald's CPS list) |
| 53 (0x35) | GPS Roaming | `key_pf_gps_roaming` |  | radio menu only (newer than reald's CPS list) |
| 54 (0x36) | DIM Shut | `key_pf_dim_shut` |  | radio menu only (newer than reald's CPS list) |
| 55 (0x37) | Predicting | `key_pf_nop` |  | radio menu only (newer than reald's CPS list), **no-op in V4.00** |
| 56 (0x38) | Sq Level | `key_pf_sq_level` |  | radio menu only (newer than reald's CPS list) |


## 8. Adding an "AM" option: where it plugs in

1. **Option text:** Band width desc 0x080A9680 (`count` 2, type 1, both languages). Point
   `items` at a new 3-entry EN/CN string array in free flash, and set `count` to 3.
2. **Storage:** `chanset_commit` case 0x0A already stores `value & 3` into +8 [5:4], so value 2
   reaches the channel record unchanged. It then calls `rf_cmd_post(1)`.
3. **RF:** `rf_cmd_apply_lo` / `at1846_load_mode_profile` must map BW=2 to an AM setup. This
   depends on the AT1846S actually having an AM path (unknown). Also audit every reader of
   +8 [5:4] that treats it as a boolean.
4. **Caveat — region 1 restricts Band width.** `chanset_build_options` forces the list to one
   entry (Narrow only) when the channel is digital **or the region byte is 1**. Region 1 is the
   one the band-unlock patch widened, so a menu-side AM option on region 1 needs that clamp
   patched too. CPS-programmed channels aren't affected.

## Scan list behaviour
Scan list is a **per-channel** codeplug field; picking one in the radio menu writes only the current channel. For "scan list follows zone", set each channel's Scan List in CPS (Channel.CSV `Scan List`; zones in Zone.CSV). A channel in several zones holds one list.
