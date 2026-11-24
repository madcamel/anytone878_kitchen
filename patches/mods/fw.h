/* Firmware symbols for mods (D878UV V4.00). Every name here is resolved at link time from
 * annotations/names.txt (functions) and annotations/globals.txt (RAM globals) by patches/modbuild.py,
 * so a declaration only works if the name exists there. Add declarations as mods need them.
 * Builder symbols: MOD_RAM / MOD_RAM_END = this mod's private RAM slice (zeroed by nothing: the
 * mod must initialise it; at boot it holds whatever was there). */
#ifndef FW_H
#define FW_H
#include <stdint.h>

extern uint8_t MOD_RAM[] __attribute__((aligned(8)));   /* the builder aligns slices to 8 */
extern uint8_t MOD_RAM_END[];

/* RAM globals */
extern volatile uint16_t adc_pc0_rx_audio;     /* RX audio, 12-bit, refreshed by ADC DMA */
extern volatile uint8_t  ana_sq_open;          /* analog squelch (+CTCSS/DCS) open */
extern volatile uint8_t  ana_rx_audio_open;
extern volatile uint8_t  tim6_audio_mode;
extern volatile uint8_t  rf_tx_active;
extern volatile uint32_t rx_freq_10hz;
extern uint8_t *volatile rf_chan_rec;
extern volatile uint8_t  aprs_rx_frame_ready;
extern volatile uint8_t  aprs_rx_in_frame;
extern volatile uint8_t  aprs_rx_frame_len;
extern volatile uint8_t  aprs_rx_frame[300];
extern volatile uint16_t aprs_tx_holdoff;
extern volatile uint8_t  aprs_board_present;

/* functions */
void tim6_audio_zero_cross_period(void);
void tim6_set_rate(uint32_t period, uint32_t prescaler);
void TIM_Cmd(void *tim, int enable);
int  chan_is_digital(void);
void aprs_rx_frame_process(void);
void sigdec_select_for_channel(void);
int  ax25_fcs_check(volatile uint8_t *start, uint32_t offset);

#define TIM6 ((void *)0x40001000)

/* --- UI / keys / display (docs/ui.md) --- */
extern volatile uint8_t  screen_id;
extern volatile uint8_t  key_code;
extern volatile uint8_t  radio_settings[256];
extern volatile uint32_t disp_dirty_bits;
extern volatile uint16_t ui_fg_color, ui_bg_color;
extern void (*volatile key_fn_table[112])(void);  /* RAM dispatch, index = key function ID */
extern void *volatile lcd_bus_sem;                 /* OS_EVENT*: LCD + keypad share PE7-14 */
void ui_key_handler(void);
void ui_render(void);
void ui_activity_wake(void);
void ptt_press(void);
void app_event_post(uint32_t bits);
void lcd_set_window(uint32_t x0, uint32_t y0, uint32_t x1, uint32_t y1);
void lcd_write_cmd(uint32_t cmd);
void lcd_text_centered(int x0, int x1, int y, int font_h, const char *s, int flag);

/* --- RF / channel (OVERVIEW "RF") --- */
extern volatile uint32_t rx_freq_a, rx_freq_b;
extern uint8_t *volatile cur_chan_rec;             /* main side's channel/VFO record */
extern void *volatile at1846_bus_sem;              /* OS_EVENT*: AT1846S bus */
uint32_t at1846_read_reg(uint32_t reg);
void at1846_write_reg(uint32_t reg, uint32_t val);
void at1846_vhf_frontend_set(int on);              /* unlocked: hold at1846_bus_sem */
void at1846_uhf_frontend_set(int on);
void sides_reload_freqs(void);
void chan_apply(void);
void rf_cmd_post(uint32_t cmd);
void freq_10hz_to_bcd(uint32_t f, uint8_t *out);
void frontend_tune_pwm_write(uint32_t v);          /* RX tracking filter (TIM3 CCR1) */
extern volatile uint16_t rf_tune_cal_vhf[39], rf_tune_cal_220[6], rf_tune_cal_uhf[13];
void key_pf_vfo_mr(void);                          /* stock VF/MR toggle of the main side (screen 0/9 only) */

/* --- uC/OS-II --- */
extern void *volatile OSTCBPrioTbl[64];
void OSSemPend(void *ev, uint32_t timeout, uint8_t *err);
uint32_t OSSemPost(void *ev);
void OSTimeDly(uint32_t ticks);                    /* 5 ms ticks */
uint32_t OSTaskCreate(void (*task)(void *), void *arg, uint32_t *ptos, uint32_t prio);
#endif
