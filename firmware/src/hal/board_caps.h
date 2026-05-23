#pragma once
#include <stdint.h>

// Runtime board description consumed by board-agnostic code (UI, main loop).
// Each board provides a single BoardCaps instance via board_caps().
//
// Compile-time-only facts (pin numbers, library choice) belong in
// boards/<name>/board.h and never leak into shared code. Anything the UI or
// main loop needs at runtime — display size, optional-feature presence —
// goes here so shared code stays free of #ifdef BOARD_*.
struct BoardCaps {
    const char* name;        // human-readable, e.g. "Waveshare AMOLED 2.16"

    // Native physical panel dimensions. Constant for the lifetime of the
    // program — never reflects rotation. For the orientation-aware "what
    // size is LVGL currently rendering at?" answer, call
    // display_hal_active_width/height() instead.
    int16_t width;
    int16_t height;

    uint8_t button_count;    // 1 = primary (BOOT) only; 2 = primary + secondary
    bool    has_rotation;    // IMU-driven CPU rotation in the flush callback
    bool    has_battery;     // AXP2101 battery measurement is meaningful
    bool    has_imu;         // QMI8658 (or compatible) is populated
};

const BoardCaps& board_caps(void);
