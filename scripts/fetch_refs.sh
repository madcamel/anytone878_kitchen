#!/bin/sh
# Download the public datasheets / guides the docs refer to into docs/ref/ (git-ignored) and make
# text copies (pdftotext -layout) for grepping. Safe to re-run.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
R=$ROOT/docs/ref; mkdir -p "$R"; cd "$R"
get() { [ -s "$1" ] || { echo "fetch $1"; curl -fsSL -o "$1" "$2" || echo "  FAILED: $2"; }; }
get RDA-1846_Programing_Guide.pdf   https://wiki.w9cr.net/images/f/f0/RDA-1846_Programing_Guide.pdf
get SCT3258_datasheet_v2_0.pdf      https://cdn.hackaday.io/files/1642237026116832/SCT3258_datasheet_v2_0.pdf
get CT3258_Packet_Interface.pdf     https://cdn.hackaday.io/files/1642237026116832/CT3258_Packet_Interface.pdf
get BK1080.pdf                      https://xn--p8jqu4215bemxd.com/wp-content/uploads/2019/08/BK1080.pdf
get ST7735S_v1.1.pdf                https://cdn.sparkfun.com/assets/9/0/2/5/8/ST7735S_v1.1.pdf
O=https://www.opengd77.com/downloads/datasheets/AT1846S
for f in AT1846S-Datasheet-0.3.pdf AT1846S-ProgrammingGuide-1.4.pdf RDA1846_Programming_manual.pdf \
	RDA1846S_datasheet_v0.1.pdf RDA1846S_programming_guide_1.2.pdf RDA1846S_register_table_20111008.xls \
	RDA1846S_SQ_algorithmic_introduction.pdf; do get "$f" "$O/$f"; done
get at1846_registers.md             https://raw.githubusercontent.com/OpenRTX/openrtx.github.io/master/at1846_registers.md
[ -d anytone-flash-tools ] || git clone -q https://github.com/reald/anytone-flash-tools.git || true
[ -d uC-OS2 ] || git clone -q https://github.com/weston-embedded/uC-OS2.git || true
# text copies, named as the docs cite them
if command -v pdftotext >/dev/null; then
	t() { [ -s "$2" ] || pdftotext -layout "$1" "$2" 2>/dev/null || true; }
	t RDA-1846_Programing_Guide.pdf rda1846_guide.txt
	t SCT3258_datasheet_v2_0.pdf sct3258_datasheet.txt
	t CT3258_Packet_Interface.pdf sct3258_packet_interface.txt
	t BK1080.pdf bk1080_datasheet.txt
	t ST7735S_v1.1.pdf st7735s_datasheet.txt
	t AT1846S-ProgrammingGuide-1.4.pdf at1846s_programming_guide.txt
	t AT1846S-Datasheet-0.3.pdf at1846s_datasheet.txt
else echo "pdftotext not found (poppler-utils): skipping text copies"; fi
if [ ! -s rda1846s_register_table.txt ] && [ -x "$ROOT/.venv/bin/python" ]; then
	"$ROOT/.venv/bin/python" - <<'PY' || true
import xlrd
wb = xlrd.open_workbook("RDA1846S_register_table_20111008.xls")
with open("rda1846s_register_table.txt", "w") as out:
    for sh in wb.sheets():
        out.write("=== sheet %s\n" % sh.name)
        for r in range(sh.nrows):
            row = [str(c.value).strip() for c in sh.row(r)]
            if any(row): out.write(" | ".join(row) + "\n")
PY
fi
echo "Not downloadable: the u-blox UBX-G7020-KT datasheet and ST's STM32F10x Standard Peripheral"
echo "Library (STSW-STM32054) need a vendor login; get them from u-blox.com / st.com if needed."
ls "$R"
