#pragma once

#define BOARD_NAME           "Waveshare ePaper 1.54"

// ---- Display geometry ----
#define LCD_WIDTH            200
#define LCD_HEIGHT           200

// ---- SSD1681 e-paper pins (SPI) ----
// TODO: verify against V2 schematic at docs.waveshare.com/ESP32-S3-ePaper-1.54
#define EPD_CS               7
#define EPD_SCLK             12
#define EPD_MOSI             11
#define EPD_DC               6
#define EPD_RST              5
#define EPD_BUSY             4

// ---- Buttons ----
#define BTN_BACK_GPIO        0     // BOOT — primary, Space (PTT)
#define BTN_PWR_GPIO         21    // TODO: verify against V2 schematic

// ---- Capability flags ----
#define BOARD_HAS_SECONDARY_BUTTON 0
#define BOARD_HAS_ROTATION         0
#define BOARD_HAS_IMU              0
#define BOARD_HAS_BATTERY          0
#define BOARD_HAS_IO_EXPANDER      0
