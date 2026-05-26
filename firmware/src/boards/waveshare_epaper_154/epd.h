#pragma once
#include <stdint.h>

// Minimal SSD1681 e-paper driver. Board-private to waveshare_epaper_154;
// not exposed in hal/. Vendored to avoid pulling in a multi-panel
// dependency for a single panel target. Mirrors the AMOLED-1.8 board's
// vendored FT3168 touch reader.
//
// Framebuffer is a 1bpp packed bitmap of LCD_WIDTH x LCD_HEIGHT pixels,
// MSB-first within each byte, stored row-major. Bit value 1 = white,
// 0 = black (SSD1681 convention).

void epd_init(void);
void epd_deep_sleep(void);

// Push the entire framebuffer using a full refresh (~2 s, flashes the
// screen to clear ghost pixels).
void epd_full_refresh(const uint8_t* framebuf);

// Push a sub-region using a partial refresh (~300 ms, no flash).
// Caller is responsible for ensuring x1 and x2 are byte-aligned
// (multiples of 8) — SSD1681 addresses pixel columns in 8-pixel bytes.
void epd_partial_refresh(const uint8_t* framebuf,
                         int x1, int y1, int x2, int y2);
