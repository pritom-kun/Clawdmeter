#include "board.h"
#include <Arduino.h>
#include <Wire.h>

// Bring up the shared I2C bus. AMOLED-2.06 has no IO expander, so this is
// all the early init needed before display/touch/power/imu HAL calls.
// LCD_RESET and TP_RST are direct GPIOs handled inside display.cpp /
// touch.cpp respectively.
extern "C" void board_init(void) {
    Wire.begin(IIC_SDA, IIC_SCL);
}
