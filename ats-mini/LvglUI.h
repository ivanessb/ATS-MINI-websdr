#ifndef LVGL_UI_H
#define LVGL_UI_H

#include <lvgl.h>

// Initialize LVGL display driver and create the main screen
void lvglInit();

// Update LVGL widget values from current radio state
void lvglUpdate();

// Call in the main loop to let LVGL process rendering
void lvglTick();

// Returns true if LVGL is managing the display
bool lvglActive();

// Enable/disable LVGL rendering (when switching to/from legacy screens)
void lvglSetActive(bool active);

#endif // LVGL_UI_H
