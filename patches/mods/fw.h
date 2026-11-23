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

#define TIM6 ((void *)0x40001000)
#endif
