#pragma once
#include <stdint.h>
#include <lvgl.h>

// Initialize splash module at the given LVGL canvas dimensions (W, H).
// Creates the container + centered square pixel canvas under `parent` and
// (re-)allocates the PSRAM backing buffer sized to min(W, H). Idempotent —
// frees any prior allocation so ui_rebuild() can call it again safely.
void splash_init(lv_obj_t *parent, int w, int h);

// Advance animation frame if hold time elapsed. Call from main loop.
void splash_tick(void);

// Cycle to the next animation in the catalog.
void splash_next(void);

// Show/hide the splash container.
void splash_show(void);
void splash_hide(void);

// Pick the next animation matching the current usage-rate group.
// Called automatically by splash_show(); also exposed so other modules can
// trigger a re-pick when the rate group changes mid-display.
void splash_pick_for_current_rate(void);

// True when splash is currently rendering (used to gate re-picks).
bool splash_is_active(void);

// Root container (so ui.cpp can attach a click event).
lv_obj_t* splash_get_root(void);

// Animation-index accessors, used by ui_rebuild() to preserve the active
// animation across a rotation-triggered teardown/rebuild. The setter is a
// no-op if idx is out of range or if no animations are loaded.
int  splash_get_animation_index(void);
void splash_set_animation_index(int idx);
