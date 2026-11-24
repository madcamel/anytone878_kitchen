/* scope: on-radio spectrum waterfall for the band the radio is tuned to (VHF / mid / UHF).
 *
 * Open:  assign key function 55 "Predicting" (a no-op in V4.00) to any PF/P key in the radio's key
 *        menu; this mod installs scope_open in key_fn_table[55]. Opens from the standby screen.
 * Keys:  knob CW/CCW (0x11/0x12) move the cursor; Up/Down (0x0F/0x10) waterfall speed (160/80/40/20
 *        measurements per row); P2 (0x14) zooms in x8 around the cursor, and at
 *        the finest level tunes the main VFO (RAM) to the cursor and exits; P1 (0x13) zooms out,
 *        exits at the full band; Exit (0x0E) exits; PTT exits (radio restored) and then transmits.
 * Screen (160x128, screen_id SCOPE_ID = 0x22, see the define for why exactly that value):
 *        y 0-15 span (or exact frequency at the finest level), y 16-43 spectrum of the latest
 *        sweep with cursor and zoom bracket, line at y 45, y 48-111 waterfall (64 sweeps, newest on
 *        top), y 112-127 cursor frequency, dB and speed.
 *
 * Hooks (task_p10_keys_ui, task_p12_display): bl ui_key_handler -> scope_key, bl ptt_press ->
 * scope_ptt, bl ui_render -> scope_render. Each passes through to stock unless the scope is open.
 * Scanning runs in its own uC/OS task (priority SCAN_PRIO, created on first open) and drives the
 * AT1846S under at1846_bus_sem: write 0x29/0x2A, RX restart via 0x30, wait 20 ms (RSSI averaging 2,
 * timed on the DWT cycle counter), check the
 * synthesizer readback, read RSSI (median of 3). Drawing happens only in p12 under lcd_bus_sem
 * (the LCD shares PE7-14 with the keypad matrix). While open: dual watch and power save paused,
 * squelch thresholds maxed (0x49), the band's RX front end on. Exit restores through the stock path
 * (sides_reload_freqs + chan_apply + rf_cmd_post(1)), done by the scan task.
 * Integer only. Measurements behind the numbers: OVERVIEW "RF path" (scanning, front ends). */
#include "fw.h"

#define SCOPE_ID   0x22          /* ui_render paints nothing for it, and no code tests for it. NOT above
                                    0x22: radio_quiesce_for_pc treats any screen_id > 0x22 as PC programming
                                    mode and freezes the OS (pc_mode_active). >= 0x21 pauses battery_monitor_step. */
_Static_assert(SCOPE_ID <= 0x22 && SCOPE_ID != 0x13 && SCOPE_ID != 0x21, "screen id collides with stock modes");
#define FN_ID      55            /* key function "Predicting" */
#define SCAN_PRIO  15
#define DWELL_MS   20            /* RSSI settle after an RX restart, with averaging AVG (calibrated
                                    against an 80 ms ground truth: no bias, ~3 dB error; stock
                                    averaging needs 25 ms) */
#define AVG        2             /* AT1846S 0x5A[11:10] RSSI averaging while scanning (stock: 3) */
#define CYC_PER_MS 120000u       /* SYSCLK 120 MHz (DWT cycle counter) */
#define SPEEDS     4             /* row = COLS >> speed measurements; Up/Down change it */
#define COLS       160
#define ROWS       64
#define WF_Y       48
#define SP_Y       16
#define SP_H       28            /* spectrum y 16..43, separator line y 45 */
#define SEP_Y      45
#define FT_Y       112
#define FINAL_SPAN 400000u       /* 10 Hz units (4 MHz): <= 25 kHz per column = finest level */
#define SLOT       2500u         /* 25 kHz sub-slot at coarse levels */
#define STEP_TUNE  1250u         /* tune rounding: 12.5 kHz */
#define DECAY      2             /* dB per sweep, peak hold at coarse levels */
#define POST_BIT   0x40000000u   /* app_event_post bit for "scope changed" */
#define MAGIC      0x31706353u   /* "Scp1" */

enum { ST_IDLE, ST_RUN, ST_STOP, ST_HALT, ST_HALTED };   /* STOP: task restores; HALT: p10 does */
enum { D_FULL = 1, D_ROW = 2, D_CUR = 4 };

struct state {
    uint32_t magic;
    volatile uint8_t state;       /* ST_* */
    volatile uint8_t dirty;       /* D_* for the renderer */
    volatile uint8_t to_standby;  /* exit: also return the screen to standby */
    volatile uint8_t cursor;      /* column 0..COLS-1 */
    volatile uint8_t level;
    uint8_t  acquired, div, nrows, head;
    uint8_t  saved_dw, saved_ps, col;
    volatile uint32_t gen;        /* bumped by every zoom: the scan restarts its sweep */
    volatile uint32_t start, span;/* view, 10 Hz units */
    uint32_t lo, hi;              /* band, 10 Hz units */
    volatile uint32_t tune;       /* != 0: tune the main VFO here on exit */
    uint32_t row_gen, rows_done, v30;
    int32_t  floor;               /* dB, smoothed noise floor (x4) */
    uint8_t  cur[COLS];           /* latest sweep, dB (0 = no data) */
    uint8_t  peak[COLS];
    uint8_t  hist[ROWS][COLS / 2];/* 4-bit palette indices, ring, newest at head */
    uint32_t stack[512] __attribute__((aligned(8)));  /* 2 KB: exit runs the stock chan_apply chain here */
    volatile uint8_t speed;       /* 0..SPEEDS-1: each measurement covers 1 << speed columns */
    uint8_t  pad;
    uint16_t v5a;                 /* stock 0x5A (RSSI averaging), restored on exit */
};
#define S ((struct state *)MOD_RAM)
_Static_assert(sizeof(struct state) <= MOD_RAM_SIZE, "struct state doesn't fit the RAM slice");
/* layout pinned for patches/mods/scope/mod.py (test reads the state by offset) */
_Static_assert(__builtin_offsetof(struct state, gen) == 16 && __builtin_offsetof(struct state, start) == 20 &&
               __builtin_offsetof(struct state, tune) == 36 && __builtin_offsetof(struct state, cur) == 56 &&
               __builtin_offsetof(struct state, hist) == 376 && __builtin_offsetof(struct state, stack) == 5496 &&
               __builtin_offsetof(struct state, speed) == 7544,
               "struct state layout changed: update mod.py");

static const uint16_t PAL[16] = {
    0x0000, 0x0008, 0x0010, 0x0018, 0x001F, 0x021F, 0x041F, 0x05FF,
    0x07FF, 0x07EF, 0x07E0, 0x5FE0, 0xAFE0, 0xFFE0, 0xFD20, 0xF800,
};

/* ---------------- helpers ---------------- */
static int running(void) { return S->magic == MAGIC && S->state == ST_RUN; }

static uint32_t colfreq(uint32_t c)  /* centre of column c */
{
    return S->start + (S->span * (2 * c + 1)) / (2 * COLS);
}

static uint32_t clamp_start(uint32_t centre, uint32_t span)
{
    uint32_t s = centre > span / 2 ? centre - span / 2 : 0;
    if (s < S->lo) s = S->lo;
    if (s + span > S->hi) s = S->hi - span;
    return s;
}

static uint32_t col_of(uint32_t f)
{
    if (f <= S->start) return 0;
    uint32_t c = (f - S->start) * COLS / S->span;
    return c >= COLS ? COLS - 1 : c;
}

static uint32_t level_of(uint32_t db)
{
    int32_t v = ((int32_t)db - (S->floor >> 2) + 2) / 3;
    return v < 0 ? 0 : v > 15 ? 15 : (uint32_t)v;
}

/* ---------------- LCD (only from p12 inside scope_render, lcd_bus_sem held) ---------------- */
#define GPIOB_BSRR (*(volatile uint32_t *)0x40010C10)
#define GPIOB_BRR  (*(volatile uint32_t *)0x40010C14)
#define GPIOE_ODR  (*(volatile uint32_t *)0x4001180C)

static void px_begin(uint32_t x0, uint32_t y0, uint32_t x1, uint32_t y1)
{
    lcd_set_window(x0, y0, x1, y1);   /* screen y -> CASET (fast axis), x -> RASET */
    lcd_write_cmd(0x2C);
    GPIOB_BRR = 1u << 12;             /* CS low */
    GPIOB_BSRR = 1u << 10;            /* D/CX = data */
}

static void px_byte(uint32_t b)
{
    GPIOE_ODR = (GPIOE_ODR & 0x807Fu) | (b << 7);
    GPIOB_BRR = 1u << 13;             /* WR pulse */
    GPIOB_BSRR = 1u << 13;
}

static void px(uint32_t c) { px_byte(c >> 8); px_byte(c & 0xFF); }
static void px_end(void) { GPIOB_BSRR = 1u << 12; }

static void fill(uint32_t x0, uint32_t y0, uint32_t x1, uint32_t y1, uint32_t c)
{
    px_begin(x0, y0, x1, y1);
    for (uint32_t n = (x1 - x0 + 1) * (y1 - y0 + 1); n; n--)
        px(c);
    px_end();
}

static char *put_mhz(char *p, uint32_t f, int dec)   /* f in 10 Hz units */
{
    uint32_t mhz = f / 100000, frac = f % 100000, d = 10000;
    char t[4];
    int n = 0;
    do { t[n++] = (char)('0' + mhz % 10); mhz /= 10; } while (mhz);
    while (n) *p++ = t[--n];
    *p++ = '.';
    for (int k = 0; k < dec; k++) { *p++ = (char)('0' + frac / d); frac %= d; d /= 10; }
    return p;
}

static void text(int y, const char *s)
{
    uint16_t fg = ui_fg_color, bg = ui_bg_color;
    fill(0, y, COLS - 1, y + 15, 0);
    ui_fg_color = 0xFFFF;
    ui_bg_color = 0x0000;
    lcd_text_centered(0, COLS - 1, y, 16, s, 0);
    ui_fg_color = fg;
    ui_bg_color = bg;
}

static void draw_header(void)
{
    char b[24], *p = b;
    if (S->span <= FINAL_SPAN) {
        p = put_mhz(p, colfreq(S->cursor), 4);
    } else {
        p = put_mhz(p, S->start, 3);
        *p++ = '-';
        p = put_mhz(p, S->start + S->span, 3);
    }
    *p++ = ' '; *p++ = 'M'; *p++ = 'H'; *p++ = 'z'; *p = 0;
    text(0, b);
}

static void draw_footer(void)
{
    char b[24], *p = b;
    uint32_t c = S->cursor, db = S->cur[c];
    p = put_mhz(p, colfreq(c), S->span <= FINAL_SPAN ? 4 : 3);
    *p++ = ' '; *p++ = ' ';
    if (db) {                                     /* RSSI register value (chip dB scale) */
        if (db >= 100) *p++ = (char)('0' + db / 100);
        if (db >= 10) *p++ = (char)('0' + db / 10 % 10);
        *p++ = (char)('0' + db % 10);
        *p++ = 'd'; *p++ = 'B';
    } else {
        *p++ = '-'; *p++ = '-';
    }
    *p++ = ' '; *p++ = ' '; *p++ = 'x';           /* waterfall speed (Up/Down) */
    *p++ = (char)('0' + (1 << S->speed));
    *p = 0;
    text(FT_Y, b);
}

static void draw_spectrum(void)
{
    uint32_t cur = S->cursor, half = S->span <= FINAL_SPAN ? 0 : COLS / 16;
    px_begin(0, SP_Y, COLS - 1, SP_Y + SP_H - 1);
    for (uint32_t c = 0; c < COLS; c++) {
        uint32_t db = S->cur[c], lv = db ? level_of(db) : 0, h = db ? (lv + 1) * SP_H / 16 : 0;
        uint32_t edge = half && (c + half == cur || c == cur + half);
        for (uint32_t y = 0; y < SP_H; y++) {
            uint32_t col;
            if (c == cur) col = 0xFFFF;
            else if (SP_H - y <= h) col = PAL[lv];
            else if (edge) col = 0x8410;
            else col = 0x0000;
            px(col);
        }
    }
    px_end();
}

static void draw_waterfall(void)
{
    px_begin(0, WF_Y, COLS - 1, WF_Y + ROWS - 1);
    for (uint32_t c = 0; c < COLS; c++) {
        for (uint32_t i = 0; i < ROWS; i++) {
            uint32_t r = (S->head + ROWS - i) % ROWS;
            uint32_t v = i < S->nrows ? (S->hist[r][c >> 1] >> ((c & 1) * 4)) & 15 : 0;
            px(PAL[v]);
        }
    }
    px_end();
}

/* ---------------- RF (scan task; bus semaphore held around chip access) ---------------- */
#define DEMCR      (*(volatile uint32_t *)0xE000EDFC)
#define DWT_CTRL   (*(volatile uint32_t *)0xE0001000)
#define DWT_CYCCNT (*(volatile uint32_t *)0xE0001004)

/* Sleep most of the dwell (other tasks run), spin the last few ms on the cycle counter for an exact
 * DWELL_MS instead of rounding up to whole 5 ms ticks. Without a running counter (emulator) the
 * OS sleep alone is used. */
static void dwell(uint32_t t0)
{
    OSTimeDly(3);                                 /* 10..15 ms */
    uint32_t now = DWT_CYCCNT;
    if (now == t0)
        return;
    while (DWT_CYCCNT - t0 < DWELL_MS * CYC_PER_MS)
        ;
}
static void bus_take(void) { uint8_t e; OSSemPend(at1846_bus_sem, 0, &e); }
static void bus_give(void) { OSSemPost(at1846_bus_sem); }

static void acquire(void)
{
    S->saved_dw = radio_settings[0x2D];
    S->saved_ps = radio_settings[0x0B];
    radio_settings[0x2D] = 0;                     /* dual watch: would retune over the scan */
    radio_settings[0x0B] = 0;                     /* power save: sleeps the front ends */
    DEMCR |= 1u << 24;                            /* TRCENA, then the DWT cycle counter */
    DWT_CTRL |= 1u;
    bus_take();
    S->v30 = at1846_read_reg(0x30) & 0xFFFF;
    S->v5a = (uint16_t)at1846_read_reg(0x5A);
    at1846_write_reg(0x5A, (S->v5a & ~0x0C00u) | (AVG << 10));
    at1846_write_reg(0x05, 0x8763);               /* normal reference mode (readback scale) */
    at1846_write_reg(0x49, 0x3FFF);               /* squelch thresholds max: keep audio shut */
    if (S->lo < 30000000u) { at1846_uhf_frontend_set(0); at1846_vhf_frontend_set(1); }
    else { at1846_vhf_frontend_set(0); at1846_uhf_frontend_set(1); }
    bus_give();
    S->acquired = 1;
}

static int main_is_vfo(void) { return radio_settings[0x15 + (radio_settings[0x2C] & 1)] == 1; }

/* Give the radio back: settings, optional tune, stock retune (front end, filter, 0x49, profile).
 * A tune goes to the main VFO only: from channel mode the stock V/M toggle runs first (needs the
 * standby screen), and if the side is still not in VFO mode the tune is dropped rather than written
 * into a memory channel. No tune: the mode is left as it was. */
static void restore(void)
{
    if (S->acquired) {
        radio_settings[0x2D] = S->saved_dw;
        radio_settings[0x0B] = S->saved_ps;
        bus_take();
        at1846_write_reg(0x5A, S->v5a);           /* the stock profile reload doesn't rewrite it */
        bus_give();
        S->acquired = 0;
    }
    uint32_t t = S->tune;
    S->tune = 0;
    if (S->to_standby && screen_id == SCOPE_ID)
        screen_id = 0;
    if (t && screen_id == 0 && !main_is_vfo())
        key_pf_vfo_mr();                          /* stock: VF/MR flag, VFO record load, reload, apply */
    if (t && cur_chan_rec && main_is_vfo()) {
        uint8_t bcd[4];                           /* RX freq of the VFO record, RAM only */
        freq_10hz_to_bcd(t, bcd);
        for (int i = 0; i < 4; i++) cur_chan_rec[i] = bcd[i];
    }
    sides_reload_freqs();
    chan_apply();                                 /* DMR reconfig + RF retune (profile, 0x49, front end) */
    rf_cmd_post(1);
    app_event_post(0xFFFFFFFFu);
}

/* RX tracking-filter PWM for f, as stock rf_frontend_tune_from_tables computes it (same tables and
 * integer maths, clamped to the calibrated edges like the band-unlock patch). Without it the filter
 * stays parked at the VFO frequency and readings far from it are skewed. */
static uint32_t interp(volatile uint16_t *t, uint32_t i, uint32_t frac)
{
    return (t[i] + ((((uint32_t)(t[i + 1] - t[i]) & 0xFFFF) * frac) & 0xFFFF) / 10) & 0xFFFF;
}

static uint32_t track_pwm(uint32_t f)
{
    uint32_t g, i;
    if (f < 31000000u) {
        if (f <= 17400000u) {
            g = (f < 13600000u ? 13600000u : f) - 13600000u;
            i = g / 100000u;
            return i < 38 ? interp(rf_tune_cal_vhf, i, g / 10000u % 10) : rf_tune_cal_vhf[38];
        }
        g = (f < 22000000u ? 22000000u : f) - 22000000u;
        i = g / 100000u;
        return i < 5 ? interp(rf_tune_cal_220, i, g / 10000u % 10) : rf_tune_cal_220[5];
    }
    g = (f < 40000000u ? 40000000u : f) - 40000000u;
    i = g / 1000000u;
    return i < 12 ? interp(rf_tune_cal_uhf, i, g / 100000u % 10) : rf_tune_cal_uhf[12];
}

/* One measurement at f (10 Hz units). Returns dB, 0 if the step was disturbed. */
static uint32_t measure(uint32_t f)
{
    uint32_t w = (f << 4) / 100, d = S->div;      /* reg 0x29/0x2A = f_kHz x 16 */
    frontend_tune_pwm_write(track_pwm(f));        /* settles within the dwell */
    bus_take();
    at1846_write_reg(0x29, w >> 16);
    at1846_write_reg(0x2A, w & 0xFFFF);
    at1846_write_reg(0x30, S->v30 & ~0x20u);      /* the synthesizer takes the word on RX start */
    at1846_write_reg(0x30, S->v30);
    uint32_t t0 = DWT_CYCCNT;
    bus_give();
    dwell(t0);
    bus_take();
    uint32_t rb = ((at1846_read_reg(0x29) & 0xFFFF) << 16) | (at1846_read_reg(0x2A) & 0xFFFF);
    uint32_t a = at1846_read_reg(0x1B) >> 8 & 0xFF, b = at1846_read_reg(0x1B) >> 8 & 0xFF,
             c = at1846_read_reg(0x1B) >> 8 & 0xFF;
    bus_give();
    /* readback = VCO/6 in 1/4424.6 kHz units: rb*6000 ~ f * div * 44246; tolerance 50 kHz */
    uint64_t got = (uint64_t)rb * 6000u, want = (uint64_t)f * d * 44246u, tol = (uint64_t)5000u * d * 44246u;
    if (got + tol < want || got > want + tol)
        return 0;
    uint32_t m = a > b ? (b > c ? b : (a > c ? c : a)) : (a > c ? a : (b > c ? c : b));
    return m ? m : 1;
}

static void finish_row(void)
{
    uint8_t hist[128];                            /* <= 160 samples per bin: fits a byte */
    for (int i = 0; i < 128; i++) hist[i] = 0;
    uint32_t n = 0;
    for (int c = 0; c < COLS; c++)
        if (S->cur[c] && S->cur[c] < 128) { hist[S->cur[c]]++; n++; }
    if (n) {                                      /* 30th percentile = noise floor estimate */
        uint32_t k = 0, want = n * 3 / 10, fl = 0;
        while (fl < 127 && k + hist[fl] <= want) k += hist[fl++];
        S->floor = S->rows_done ? (S->floor * 3 + (int32_t)fl * 4) / 4 : (int32_t)fl * 4;
    }
    uint32_t h = (S->head + 1) % ROWS;
    for (int c = 0; c < COLS; c += 2) {
        uint32_t l0 = S->cur[c] ? level_of(S->cur[c]) : 0, l1 = S->cur[c + 1] ? level_of(S->cur[c + 1]) : 0;
        S->hist[h][c >> 1] = (uint8_t)(l0 | l1 << 4);
    }
    S->head = (uint8_t)h;
    if (S->nrows < ROWS) S->nrows++;
    S->rows_done++;
    S->dirty |= D_ROW;
    app_event_post(POST_BIT);
}

/* One column of the current sweep. Coarse levels sample a different 25 kHz slot of each column
 * every sweep and hold peaks, so steady stations fill in and stay; the finest level is direct. */
void scope_scan_step(void)
{
    uint32_t g = S->gen;
    if (g != S->row_gen) {                        /* zoom changed: new view, fresh history */
        S->row_gen = g;
        S->col = 0;
        S->nrows = 0;
        S->rows_done = 0;
        for (int c = 0; c < COLS; c++) { S->cur[c] = 0; S->peak[c] = 0; }
    }
    if (!S->acquired) acquire();
    uint32_t st = 1u << S->speed, c = S->col, colw = S->span / COLS, k = colw * st / SLOT;
    uint32_t f;
    if (S->span <= FINAL_SPAN || k <= 1) f = S->start + (S->span * (2 * c + st)) / (2 * COLS);
    else f = S->start + c * colw + ((S->rows_done * 7 + c / st) % k) * SLOT + SLOT / 2;
    uint32_t db = measure(f);
    if (S->gen != g) return;                      /* zoomed / speed changed meanwhile: drop */
    uint32_t v = db;
    if (S->span > FINAL_SPAN) {                   /* coarse: peak hold with decay */
        uint32_t p = S->peak[c] > DECAY ? S->peak[c] - DECAY : 0;
        v = db > p ? db : p;
    } else if (!db) {
        v = S->cur[c];                            /* disturbed step: keep the last value */
    }
    for (uint32_t i = c; i < c + st && i < COLS; i++) {
        S->peak[i] = (uint8_t)v;
        S->cur[i] = (uint8_t)v;
    }
    if (S->cursor >= c && S->cursor < c + st) S->dirty |= D_CUR;
    S->col = (uint8_t)(c + st);
    if (S->col >= COLS) { S->col = 0; finish_row(); }
}


/* One iteration of the scan task (exported for the emulation test). */
void scope_service(void)
{
    if (S->magic != MAGIC) { OSTimeDly(20); return; }
    if (S->state == ST_RUN) {
        if (rf_tx_active) { S->to_standby = 1; S->state = ST_STOP; return; }
        scope_scan_step();
    } else if (S->state == ST_STOP) {
        restore();
        S->state = ST_IDLE;
    } else if (S->state == ST_HALT) {
        S->state = ST_HALTED;                     /* between measurements: p10 may take over */
    } else {
        OSTimeDly(10);
    }
}

static void scope_task(void *arg)
{
    (void)arg;
    for (;;)
        scope_service();
}

/* ---------------- entry / exit / keys (task_p10) ---------------- */
void scope_open(void)
{
    if (screen_id != 0 || rf_tx_active)
        return;
    if (S->magic != MAGIC || OSTCBPrioTbl[SCAN_PRIO] == 0) {   /* cold start or after a reset */
        uint8_t *p = (uint8_t *)S;
        for (uint32_t i = 0; i < __builtin_offsetof(struct state, stack); i++) p[i] = 0;
        S->speed = 1;
        S->magic = MAGIC;
    }
    if (S->state != ST_IDLE)
        return;
    uint32_t f = radio_settings[0x2C] & 1 ? rx_freq_b : rx_freq_a;
    if (f < 20000000u)      { S->lo = 13100000u; S->hi = 17800000u; S->div = 6; }
    else if (f < 30000000u) { S->lo = 20000000u; S->hi = 28600000u; S->div = 4; }
    else                    { S->lo = 39300000u; S->hi = 57200000u; S->div = 2; }
    S->start = S->lo;
    S->span = S->hi - S->lo;
    S->level = 0;
    S->cursor = (uint8_t)col_of(f);
    S->tune = 0;
    S->to_standby = 0;
    S->acquired = 0;
    S->floor = 30 * 4;
    if (S->speed >= SPEEDS) S->speed = 1;          /* default 80 measurements per row; kept between opens */
    S->gen++;
    S->dirty = D_FULL;
    if (OSTCBPrioTbl[SCAN_PRIO] == 0)
        OSTaskCreate(scope_task, 0, &S->stack[511], SCAN_PRIO);
    screen_id = SCOPE_ID;
    S->state = ST_RUN;
    app_event_post(0x80000000u | POST_BIT);
}

/* Exit from p10 (P1 / Exit / tune / PTT): halt the scan task between measurements, then restore
 * here, in the key task, where the stock V/M toggle and screen changes belong. */
static void exit_now(uint32_t tune)
{
    if (S->state == ST_RUN) {
        S->state = ST_HALT;
        for (int i = 0; i < 40 && S->state != ST_HALTED; i++)
            OSTimeDly(1);                         /* <= one measurement (~30 ms) */
    }
    S->tune = tune;
    S->to_standby = 1;
    restore();
    S->state = ST_IDLE;
}

static void zoom(int in)
{
    uint32_t centre = colfreq(S->cursor), span = S->span;
    if (in) {
        if (span <= FINAL_SPAN) {                 /* finest level: tune here and leave */
            exit_now((centre + STEP_TUNE / 2) / STEP_TUNE * STEP_TUNE);
            return;
        }
        S->level++;
    } else {
        if (S->level == 0) { exit_now(0); return; }
        S->level--;
    }
    span = (S->hi - S->lo) >> (3 * S->level);     /* band / 8^level: exact both ways */
    S->span = span;
    S->start = clamp_start(centre, span);
    S->cursor = (uint8_t)col_of(centre);
    S->gen++;
    S->dirty = D_FULL;
}

static void install(void)
{
    if (key_fn_table[FN_ID] != scope_open)
        key_fn_table[FN_ID] = scope_open;
}

void scope_key(void)
{
    install();
    if (S->magic == MAGIC && S->state == ST_STOP && screen_id == SCOPE_ID) {
        restore();                                /* exit pending and the scan task hasn't taken it: */
        S->state = ST_IDLE;                       /* the user pressed again, so do it here (p10) */
        return;
    }
    if (!running() || screen_id != SCOPE_ID) {
        ui_key_handler();
        return;
    }
    ui_activity_wake();                           /* stock does this first: backlight, power save */
    switch (key_code) {
    case 0x11: if (S->cursor < COLS - 1) S->cursor++; S->dirty |= D_CUR; break;
    case 0x12: if (S->cursor > 0) S->cursor--; S->dirty |= D_CUR; break;
    case 0x0F:                                    /* Up: faster waterfall (fewer, wider columns) */
    case 0x10:                                    /* Down: slower, full resolution */
        if (key_code == 0x0F ? S->speed + 1 < SPEEDS : S->speed > 0) {
            S->speed = (uint8_t)(key_code == 0x0F ? S->speed + 1 : S->speed - 1);
            S->gen++;
            S->dirty = D_FULL;
        }
        break;
    case 0x14: zoom(1); if (S->state != ST_RUN) return; break;   /* P2 */
    case 0x13: zoom(0); if (S->state != ST_RUN) return; break;   /* P1 */
    case 0x0E: exit_now(0); return;               /* Exit */
    default: break;
    }
    app_event_post(POST_BIT);
}

void scope_ptt(void)
{
    if (S->magic == MAGIC && S->state != ST_IDLE)
        exit_now(0);                              /* scan halted, radio restored, then stock TX */
    ptt_press();
}

/* ---------------- display (task_p12) ---------------- */
void scope_render(void)
{
    install();
    if (!running()) {
        ui_render();
        return;
    }
    if (screen_id != SCOPE_ID) {                  /* stock took the screen (call alert, popup) */
        S->to_standby = 0;
        S->state = ST_STOP;
        ui_render();
        return;
    }
    uint8_t e, d = S->dirty;
    S->dirty = 0;
    if (disp_dirty_bits & 0x80000000u) d |= D_FULL;
    OSSemPend(lcd_bus_sem, 0, &e);
    if (d & D_FULL) {
        fill(0, 0, COLS - 1, 127, 0);
        fill(0, SEP_Y, COLS - 1, SEP_Y, 0x8410);  /* line between spectrum and waterfall */
        draw_header();
        draw_spectrum();
        draw_waterfall();
        draw_footer();
    } else {
        if (d & (D_ROW | D_CUR)) draw_spectrum();
        if (d & D_ROW) draw_waterfall();
        if (d & D_CUR) { draw_footer(); if (S->span <= FINAL_SPAN) draw_header(); }
    }
    OSSemPost(lcd_bus_sem);
    disp_dirty_bits = 0;
}
