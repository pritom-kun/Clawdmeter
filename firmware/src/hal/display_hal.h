#pragma once
#include <stdint.h>

// Display abstraction. The board provides the QSPI bus, panel driver, and any
// CPU-side rotation. Shared code (main.cpp, LVGL glue) never sees the GFX
// driver type. Native dimensions live in board_caps(); orientation-aware
// dimensions live in display_hal_active_width/height() below.

// Construct bus + driver objects. Safe to call before display_hal_begin().
// On boards with an IO expander gating the LCD reset, the board's
// implementation is responsible for ensuring the expander has released the
// reset before talking to the panel.
void display_hal_init(void);

// Bring the panel out of reset, clear it, and apply default brightness.
void display_hal_begin(void);

void display_hal_set_brightness(uint8_t level);  // 0..255 (driver-defined scale)
void display_hal_fill_screen(uint16_t color565);

// Write a w×h RGB565 bitmap at (x, y). Boards with software rotation
// (e.g. CO5300) transform (x, y, w, h) and the pixel buffer here before
// pushing to the panel. Shared LVGL flush_cb just calls this — no #ifdef.
void display_hal_draw_bitmap(int32_t x, int32_t y, int32_t w, int32_t h,
                             const uint16_t* pixels);

// Per-loop housekeeping for rotation-aware boards: detects orientation
// changes from the IMU, blanks the panel, and ramps brightness back up.
// On a non-square panel this also raises an internal flag that
// display_hal_consume_rotation_change() will drain on the next loop so
// shared code can call lv_display_set_resolution() and ui_rebuild() to
// match the new orientation. No-op on boards without rotation.
void display_hal_tick(void);

// Orientation-aware effective LVGL dimensions. On boards without rotation
// (or with a square panel) these always equal the native width/height. On
// rotation-enabled non-square boards they swap on quadrants 1 and 3 to
// reflect the current IMU-latched orientation.
int16_t display_hal_active_width(void);
int16_t display_hal_active_height(void);

// One-shot probe: returns true exactly once after each rotation transition
// (drains an internal flag set inside display_hal_tick). Shared code calls
// this every loop and, when it returns true, calls lv_display_set_resolution
// and ui_rebuild() so widgets re-layout for the new orientation. Always
// returns false on boards without rotation.
bool display_hal_consume_rotation_change(void);

// LVGL flush regions must be even-aligned on the CO5300; harmless on others.
void display_hal_round_area(int32_t* x1, int32_t* y1, int32_t* x2, int32_t* y2);
