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

// Set the RAM X window using byte-column addressing.
// bx1, bx2 are byte column indices (0 = pixels 0-7, 1 = pixels 8-15, ...).
// y1, y2 are row indices.
static void set_window(int bx1, int y1, int bx2, int y2) {
    write_cmd(0x44);          // cmd 0x44 — set RAM X address start/end
    write_data((uint8_t)bx1);
    write_data((uint8_t)bx2);

    write_cmd(0x45);          // cmd 0x45 — set RAM Y address start/end
    write_data((uint8_t)(y1 & 0xFF));
    write_data((uint8_t)((y1 >> 8) & 0x01));
    write_data((uint8_t)(y2 & 0xFF));
    write_data((uint8_t)((y2 >> 8) & 0x01));
}

// Set the RAM cursor to (bx, y).
static void set_cursor(int bx, int y) {
    write_cmd(0x4E);          // cmd 0x4E — set RAM X address counter
    write_data((uint8_t)bx);

    write_cmd(0x4F);          // cmd 0x4F — set RAM Y address counter
    write_data((uint8_t)(y & 0xFF));
    write_data((uint8_t)((y >> 8) & 0x01));
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void epd_init(void) {
    // Enable the panel's onboard regulator FIRST (active-LOW). Without
    // this the SSD1681 has no Vcc; SPI transactions are silently lost
    // and the panel keeps showing whatever it had at power-on. The
    // reference driver in waveshareteam/ESP32-S3-ePaper-1.54's
    // 07_BATT_PWR_Test/src/power/board_power_bsp.cpp confirms the
    // active-low convention: `gpio_set_level(epd_power_pin, 0)` = ON.
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

    // Hardware reset: pulse RST LOW for 10 ms, then HIGH for 10 ms.
    digitalWrite(EPD_RST, LOW);   delay(10);
    digitalWrite(EPD_RST, HIGH);  delay(10);
    wait_busy();

    // Software reset — resets all internal registers to defaults.
    write_cmd(0x12);              // cmd 0x12 — SW reset
    wait_busy();

    // Driver output control: gate = LCD_HEIGHT-1 = 199.
    write_cmd(0x01);              // cmd 0x01 — driver output control
    write_data((LCD_HEIGHT - 1) & 0xFF);
    write_data(((LCD_HEIGHT - 1) >> 8) & 0xFF);
    write_data(0x00);             //   GD=0, SM=0, TB=0

    // Data entry mode: X+, Y+ increment; X-major scanning.
    write_cmd(0x11);              // cmd 0x11 — data entry mode
    write_data(0x03);             //   AM=1 (X direction), IX=1, IY=1

    // Set RAM window to full panel.
    set_window(0, 0, (LCD_WIDTH / 8) - 1, LCD_HEIGHT - 1);

    // Set cursor to (0, 0).
    set_cursor(0, 0);

    // Border waveform: VBD = VSH1, held white between refreshes.
    write_cmd(0x3C);              // cmd 0x3C — border waveform control
    write_data(0x05);

    // Display update control 1: normal / bypass LUT.
    write_cmd(0x21);              // cmd 0x21 — display update control 1
    write_data(0x00);
    write_data(0x80);

    // Use internal temperature sensor.
    write_cmd(0x18);              // cmd 0x18 — temperature sensor selection
    write_data(0x80);             //   0x80 = internal sensor

    wait_busy();
}

void epd_full_refresh(const uint8_t* framebuf) {
    set_window(0, 0, (LCD_WIDTH / 8) - 1, LCD_HEIGHT - 1);
    set_cursor(0, 0);

    write_cmd(0x24);              // cmd 0x24 — write black/white RAM
    write_data_n(framebuf, FB_BYTES);

    write_cmd(0x22);              // cmd 0x22 — display update control 2
    write_data(0xF7);             //   0xF7 = full refresh sequence (clock on + load LUT + display + off)

    write_cmd(0x20);              // cmd 0x20 — master activation
    wait_busy();
}

void epd_partial_refresh(const uint8_t* framebuf,
                         int x1, int y1, int x2, int y2) {
    // Convert pixel x-coordinates to byte-column coordinates.
    int bx1 = x1 / 8;
    int bx2 = x2 / 8;

    set_window(bx1, y1, bx2, y2);
    set_cursor(bx1, y1);

    write_cmd(0x24);              // cmd 0x24 — write black/white RAM (partial region)
    int row_bytes = bx2 - bx1 + 1;
    for (int y = y1; y <= y2; y++) {
        write_data_n(&framebuf[y * (LCD_WIDTH / 8) + bx1], row_bytes);
    }

    write_cmd(0x22);              // cmd 0x22 — display update control 2
    write_data(0xFF);             //   0xFF = partial refresh sequence

    write_cmd(0x20);              // cmd 0x20 — master activation
    wait_busy();
}

void epd_deep_sleep(void) {
    write_cmd(0x10);              // cmd 0x10 — deep sleep mode
    write_data(0x01);             //   mode 1: retain RAM contents
}
