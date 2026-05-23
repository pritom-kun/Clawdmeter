#pragma once

// Waveshare ESP32-S3-Touch-AMOLED-2.06 — 2.06" portrait AMOLED watch board.
// 410x502 CO5300 + FT3168 touch + AXP2101 PMU + QMI8658 IMU.
// No IO expander — display and touch resets are direct GPIOs.
// 32 MB flash, 8 MB OPI PSRAM. IMU-driven CPU rotation is enabled and uses
// the aspect-swap path (effective LVGL W/H swap on quadrants 1/3, with a
// rebuild of every widget driven from main.cpp).

#define BOARD_NAME           "Waveshare AMOLED 2.06"

// ---- Display geometry (native portrait; orientation-aware dims are
// exposed via display_hal_active_width/height) ----
#define LCD_WIDTH            410
#define LCD_HEIGHT           502

// ---- QSPI display pins (CO5300) ----
#define LCD_CS               12
#define LCD_SCLK             11
#define LCD_SDIO0            4
#define LCD_SDIO1            5
#define LCD_SDIO2            6
#define LCD_SDIO3            7
#define LCD_RESET            8     // direct GPIO (no IO expander on this board)

// ---- I2C bus (touch + PMU + IMU all share one bus) ----
#define IIC_SDA              15
#define IIC_SCL              14

// ---- Touch (FT3168 via vendored minimal I2C reader) ----
#define TP_INT               38
#define TP_RST               9     // direct GPIO (no IO expander)
#define FT3168_ADDR          0x38

// ---- PMU ----
#define AXP2101_ADDR         0x34

// ---- Buttons ----
#define BTN_BACK_GPIO        0     // BOOT — primary, Space (PTT)
// PWR comes via AXP2101 PKEY short-press IRQ; there is no secondary button.

// ---- Capability flags ----
#define BOARD_HAS_SECONDARY_BUTTON 0
#define BOARD_HAS_ROTATION         1
#define BOARD_HAS_IMU              1
#define BOARD_HAS_BATTERY          1
#define BOARD_HAS_IO_EXPANDER      0
