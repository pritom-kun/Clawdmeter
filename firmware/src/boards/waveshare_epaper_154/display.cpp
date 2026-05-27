#include "../../hal/display_hal.h"
#include "board.h"
#include "epd.h"
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <string.h>

// 200x200 / 8 = 5000-byte 1bpp framebuffer (MSB-first, row-major).
#define FB_BYTES  ((LCD_WIDTH * LCD_HEIGHT) / 8)

// Coalesce multi-region LVGL flushes from the same animation tick.
// Must be SHORTER than the e-paper UI cadences (4s spinner, 8s message)
// so settle reliably fires between ticks.
#define SETTLE_MS  100

// Full refresh trigger: fire whichever fires first.
#define FULL_REFRESH_INTERVAL_MS  60000  // time-based ghost clearance
#define MAX_PARTIALS_BEFORE_FULL  30     // safety override on activity bursts

static uint8_t* framebuf = nullptr;
static bool     dirty = false;
static uint32_t last_flush_ms = 0;
static uint32_t last_full_refresh_ms = 0;
static uint32_t partial_count = 0;
static int      dirty_x1 = 0, dirty_y1 = 0, dirty_x2 = 0, dirty_y2 = 0;

// Threshold an RGB565 pixel to 1bpp (1=white, 0=black) via BT.601 luminance.
// Coefficients (76, 150, 30) are Q8 approximations of 0.299/0.587/0.114.
static inline bool rgb565_is_white(uint16_t p) {
    uint8_t r = (p >> 11) & 0x1F;
    uint8_t g = (p >>  5) & 0x3F;
    uint8_t b =  p        & 0x1F;
    uint16_t lum = (uint16_t)((r << 3) * 76 + (g << 2) * 150 + (b << 3) * 30);
    return (lum >> 8) >= 128;
}

static inline void expand_dirty(int x, int y, int w, int h) {
    int x2 = x + w - 1;
    int y2 = y + h - 1;
    if (!dirty) {
        dirty_x1 = x;  dirty_y1 = y;
        dirty_x2 = x2; dirty_y2 = y2;
        dirty = true;
    } else {
        if (x  < dirty_x1) dirty_x1 = x;
        if (y  < dirty_y1) dirty_y1 = y;
        if (x2 > dirty_x2) dirty_x2 = x2;
        if (y2 > dirty_y2) dirty_y2 = y2;
    }
}

void display_hal_init(void) {
    framebuf = (uint8_t*)heap_caps_malloc(FB_BYTES, MALLOC_CAP_INTERNAL);
    if (framebuf) memset(framebuf, 0xFF, FB_BYTES);  // start all-white
    epd_init();
}

void display_hal_begin(void) {
    if (framebuf) {
        epd_full_refresh(framebuf);
        last_full_refresh_ms = millis();
    }
}

void display_hal_set_brightness(uint8_t level) {
    (void)level;   // e-paper has no backlight.
}

void display_hal_fill_screen(uint16_t color565) {
    if (!framebuf) return;
    memset(framebuf, rgb565_is_white(color565) ? 0xFF : 0x00, FB_BYTES);
    expand_dirty(0, 0, LCD_WIDTH, LCD_HEIGHT);
    last_flush_ms = millis();
}

void display_hal_draw_bitmap(int32_t x, int32_t y, int32_t w, int32_t h,
                             const uint16_t* pixels) {
    if (!framebuf || !pixels) return;
    for (int32_t row = 0; row < h; row++) {
        int32_t py = y + row;
        if (py < 0 || py >= LCD_HEIGHT) continue;
        for (int32_t col = 0; col < w; col++) {
            int32_t px = x + col;
            if (px < 0 || px >= LCD_WIDTH) continue;
            uint8_t bit  = 1 << (7 - (px & 7));
            uint8_t* byte = &framebuf[py * (LCD_WIDTH / 8) + (px >> 3)];
            // INVERTED for e-paper: the shared UI uses a dark theme
            // (THEME_BG=0x000000, THEME_TEXT=0xfaf9f5) which is natural
            // on AMOLED but unnatural on e-paper. Map LVGL-light pixels
            // (text/icons) to panel-black, and LVGL-dark pixels
            // (background) to panel-white — gives black-on-white output
            // on the panel without touching shared UI code.
            if (rgb565_is_white(pixels[row * w + col])) *byte &= ~bit;
            else                                         *byte |=  bit;
        }
    }
    expand_dirty(x, y, w, h);
    last_flush_ms = millis();
}

void display_hal_tick(void) {
    if (!dirty || !framebuf) return;
    if (millis() - last_flush_ms < SETTLE_MS) return;

    // Clamp dirty region into panel bounds.
    if (dirty_x1 < 0) dirty_x1 = 0;
    if (dirty_y1 < 0) dirty_y1 = 0;
    if (dirty_x2 >= LCD_WIDTH)  dirty_x2 = LCD_WIDTH - 1;
    if (dirty_y2 >= LCD_HEIGHT) dirty_y2 = LCD_HEIGHT - 1;

    uint32_t now = millis();
    bool time_for_full  = (now - last_full_refresh_ms) >= FULL_REFRESH_INTERVAL_MS;
    bool count_for_full = partial_count >= MAX_PARTIALS_BEFORE_FULL;

    if (time_for_full || count_for_full) {
        epd_full_refresh(framebuf);
        last_full_refresh_ms = now;
        partial_count = 0;
    } else {
        epd_partial_refresh(framebuf, dirty_x1, dirty_y1, dirty_x2, dirty_y2);
        partial_count++;
    }
    dirty = false;
}

void display_hal_round_area(int32_t* x1, int32_t* y1, int32_t* x2, int32_t* y2) {
    // SSD1681 addresses pixel columns in 8-pixel bytes.
    *x1 = (*x1) & ~7;
    *x2 = (*x2) | 7;
    if (*x1 < 0) *x1 = 0;
    if (*x2 >= LCD_WIDTH) *x2 = LCD_WIDTH - 1;
}
