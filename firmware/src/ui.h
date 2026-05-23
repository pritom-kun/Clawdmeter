#pragma once
#include "data.h"
#include "ble.h"

enum screen_t {
    SCREEN_SPLASH,
    SCREEN_USAGE,
    SCREEN_BLUETOOTH,
    SCREEN_COUNT,
};

void ui_init(void);

// Tear down every widget on the active screen and rebuild them at the
// current display_hal_active_width/height(). Used by main.cpp after a
// rotation transition on non-square panels. State that survives the
// rebuild: current screen, last BLE state, last battery sample, and
// (via splash_get_animation_index) the active splash animation cell.
void ui_rebuild(void);

void ui_update(const UsageData* data);
void ui_tick_anim(void);
void ui_show_screen(screen_t screen);
void ui_cycle_screen(void);
void ui_toggle_splash(void);
screen_t ui_get_current_screen(void);
void ui_update_ble_status(ble_state_t state, const char* name, const char* mac);
void ui_update_battery(int percent, bool charging);
