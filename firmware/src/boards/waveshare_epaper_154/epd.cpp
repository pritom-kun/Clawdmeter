#include "epd.h"
#include "board.h"
#include <Arduino.h>
#include <SPI.h>

// 1bpp framebuffer size: 200 x 200 / 8 = 5000 bytes
#define FB_BYTES  ((LCD_WIDTH * LCD_HEIGHT) / 8)

// Arduino Core 3.x (pioarduino) removed the VSPI/HSPI aliases; use FSPI
// (the secondary SPI peripheral on S3) since the default SPI object is
// already in use by the framework. FSPI maps to the same peripheral as
// the old VSPI on S3.
static SPIClass         spi_bus(FSPI);
static SPISettings      spi_cfg(4000000, MSBFIRST, SPI_MODE0);

// ---------------------------------------------------------------------------
// Waveform LUT for the Waveshare 1.54" V2 SSD1681 panel
// ---------------------------------------------------------------------------
// Sourced verbatim from waveshareteam/ESP32-S3-ePaper-1.54
// (02_Example/Arduino/07_BATT_PWR_Test/src/display/epaper_driver_bsp.cpp,
//  symbol WF_Full_1IN54). Loading this explicitly is mandatory: the chip's
// OTP LUT is unreliable on this panel revision — refreshes against it
// produce salt-and-pepper noise instead of clean B/W output. The first
// 153 bytes go to cmd 0x32; the remaining 6 are trailer settings for
// cmds 0x3F, 0x03, 0x04 (3 bytes), and 0x2C.
static const uint8_t WF_FULL[159] = {
    0x80,0x48,0x40,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x40,0x48,0x80,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x80,0x48,0x40,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x40,0x48,0x80,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x0A,0x00,0x00,0x00,0x00,0x00,0x00,
    0x08,0x01,0x00,0x08,0x01,0x00,0x02,
    0x0A,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    0x22,0x22,0x22,0x22,0x22,0x22,0x00,0x00,0x00,
    0x22,0x17,0x41,0x00,0x32,0x20
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

// Poll EPD_BUSY until LOW (idle). SSD1681 signals busy = HIGH.
// Returns true if idle; false on 5-second timeout.
static bool wait_busy(void) {
    uint32_t deadline = millis() + 5000;
    while (digitalRead(EPD_BUSY) == HIGH) {
        if (millis() > deadline) return false;
        delay(1);
    }
    return true;
}

// Send a command byte (DC = LOW).
static void write_cmd(uint8_t cmd) {
    digitalWrite(EPD_DC, LOW);
    spi_bus.beginTransaction(spi_cfg);
    digitalWrite(EPD_CS, LOW);
    spi_bus.transfer(cmd);
    digitalWrite(EPD_CS, HIGH);
    spi_bus.endTransaction();
}

// Send a single data byte (DC = HIGH).
static void write_data(uint8_t data) {
    digitalWrite(EPD_DC, HIGH);
    spi_bus.beginTransaction(spi_cfg);
    digitalWrite(EPD_CS, LOW);
    spi_bus.transfer(data);
    digitalWrite(EPD_CS, HIGH);
    spi_bus.endTransaction();
}

// Send a multi-byte data buffer (DC = HIGH).
static void write_data_n(const uint8_t* buf, size_t len) {
    if (!buf || len == 0) return;
    digitalWrite(EPD_DC, HIGH);
    spi_bus.beginTransaction(spi_cfg);
    digitalWrite(EPD_CS, LOW);
    spi_bus.writeBytes(buf, len);
    digitalWrite(EPD_CS, HIGH);
    spi_bus.endTransaction();
}

// Reference's EPD_SetWindows: X is divided by 8 (byte addressing),
// Y is sent as 16-bit pixel address. Args are pixel coordinates.
static void set_window_px(uint16_t x_start_px, uint16_t y_start_px,
                          uint16_t x_end_px,   uint16_t y_end_px) {
    write_cmd(0x44);
    write_data((x_start_px >> 3) & 0xFF);
    write_data((x_end_px   >> 3) & 0xFF);

    write_cmd(0x45);
    write_data( y_start_px       & 0xFF);
    write_data((y_start_px >> 8) & 0xFF);
    write_data( y_end_px         & 0xFF);
    write_data((y_end_px   >> 8) & 0xFF);
}

// Reference's EPD_SetCursor: X is 8-bit (byte position), Y is 16-bit.
// Args are pixel coordinates; X gets divided by 8.
static void set_cursor_px(uint16_t x_px, uint16_t y_px) {
    write_cmd(0x4E);
    write_data((x_px >> 3) & 0xFF);

    write_cmd(0x4F);
    write_data( y_px       & 0xFF);
    write_data((y_px >> 8) & 0xFF);
}

// Load the panel-specific waveform LUT. Mirrors reference's EPD_SetLut:
// 153 bytes via cmd 0x32, then 6 trailer bytes to cmds 0x3F, 0x03,
// 0x04 (×3), 0x2C.
static void load_lut(const uint8_t* lut) {
    write_cmd(0x32);
    write_data_n(lut, 153);
    wait_busy();

    write_cmd(0x3F); write_data(lut[153]);
    write_cmd(0x03); write_data(lut[154]);
    write_cmd(0x04); write_data(lut[155]); write_data(lut[156]); write_data(lut[157]);
    write_cmd(0x2C); write_data(lut[158]);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void epd_init(void) {
    // Enable the panel's onboard regulator FIRST (active-LOW). Without
    // this the SSD1681 has no Vcc and silently swallows all SPI traffic.
    pinMode(EPD_PWR, OUTPUT);
    digitalWrite(EPD_PWR, LOW);
    delay(10);   // let Vcc settle before clocking SCLK/CS

    // Bring up SPI bus (MISO unused — SSD1681 is write-only).
    spi_bus.begin(EPD_SCLK, /*MISO=*/-1, EPD_MOSI, /*SS=*/-1);

    // Configure control pins.
    pinMode(EPD_CS,   OUTPUT);  digitalWrite(EPD_CS,   HIGH);
    pinMode(EPD_DC,   OUTPUT);  digitalWrite(EPD_DC,   HIGH);
    pinMode(EPD_RST,  OUTPUT);  digitalWrite(EPD_RST,  HIGH);
    pinMode(EPD_BUSY, INPUT);

    // Hardware reset, timing matches reference's EPD_Init exactly.
    digitalWrite(EPD_RST, HIGH); delay(50);
    digitalWrite(EPD_RST, LOW);  delay(20);
    digitalWrite(EPD_RST, HIGH); delay(50);
    wait_busy();

    write_cmd(0x12);             // SW reset
    wait_busy();

    // Driver output control — A[8:0]=LCD_HEIGHT-1, B[2:0]=0x01 (TB=1).
    // Reference: EPD_SendData(0xC7); EPD_SendData(0x00); EPD_SendData(0x01);
    write_cmd(0x01);
    write_data((LCD_HEIGHT - 1) & 0xFF);
    write_data(((LCD_HEIGHT - 1) >> 8) & 0xFF);
    write_data(0x01);

    // Data entry mode 0x01: X+ increment, Y- decrement, X-major. Combined
    // with set_window_px(0, Height-1, Width-1, 0) and set_cursor_px(0,
    // Height-1) below, this yields a RAM walk equivalent to top-down
    // physical scan when TB=1 (the gate-driver direction matches the Y-
    // decrement). Matches the reference call sequence verbatim.
    write_cmd(0x11);
    write_data(0x01);

    // Reference: EPD_SetWindows(0, Width-1, Height-1, 0)
    //   (Xstart=0, Ystart=Height-1, Xend=Width-1, Yend=0)
    set_window_px(0, LCD_HEIGHT - 1, LCD_WIDTH - 1, 0);

    // Border waveform — reference value (0x01, not 0x05 like our old code).
    write_cmd(0x3C);
    write_data(0x01);

    // Use internal temperature sensor.
    write_cmd(0x18);
    write_data(0x80);

    // Load temperature + waveform from OTP, then activate. The reference
    // does this even though the OTP LUT will get overridden by load_lut
    // below — skipping it left some panels in an indeterminate state.
    write_cmd(0x22);
    write_data(0xB1);
    write_cmd(0x20);

    set_cursor_px(0, LCD_HEIGHT - 1);
    wait_busy();

    // Explicitly load the panel-specific full-refresh LUT. The OTP LUT
    // is unreliable on this revision — without this step the panel
    // renders salt-and-pepper noise.
    load_lut(WF_FULL);
}

void epd_full_refresh(const uint8_t* framebuf) {
    set_window_px(0, LCD_HEIGHT - 1, LCD_WIDTH - 1, 0);
    set_cursor_px(0, LCD_HEIGHT - 1);

    write_cmd(0x24);                // write B/W RAM
    write_data_n(framebuf, FB_BYTES);

    // Display update control 2 = 0xC7: enable clock + enable analog +
    // display mode 1 + disable analog + disable clock. NO "load LUT"
    // bit — the LUT was loaded in epd_init() and persists. Matches
    // reference's EPD_TurnOnDisplay verbatim.
    write_cmd(0x22);
    write_data(0xC7);
    write_cmd(0x20);
    wait_busy();
}

void epd_partial_refresh(const uint8_t* framebuf,
                         int x1, int y1, int x2, int y2) {
    // Partial refresh needs its own LUT (WF_PARTIAL_1IN54_0 in the
    // reference) plus a different 0x37/0x3C/0x22 setup pass. We have
    // the LUT bytes documented but haven't wired up the partial path
    // yet — for now, fall back to a full refresh so output is always
    // clean. Cost: ~1.5 s + a visible flicker per call. The dirty-
    // rectangle args are unused in this path; the full framebuffer
    // is sent.
    (void)x1; (void)y1; (void)x2; (void)y2;
    epd_full_refresh(framebuf);
}

void epd_deep_sleep(void) {
    write_cmd(0x10);                // deep sleep mode
    write_data(0x01);               //   mode 1: retain RAM contents
}
