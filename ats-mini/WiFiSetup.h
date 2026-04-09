#ifndef WIFISETUP_H
#define WIFISETUP_H

#include <stdint.h>

// WiFi setup states
#define WIFISETUP_LIST      0  // Show saved networks + scan option
#define WIFISETUP_SCANNING  1  // Scanning in progress
#define WIFISETUP_RESULTS   2  // Show scan results
#define WIFISETUP_PASSWORD  3  // Password entry
#define WIFISETUP_MAX_SCAN 15  // Maximum scan results to display
#define WIFISETUP_MAX_PASS 64  // Maximum password length

void wifiSetupEnter();
void wifiSetupExit();
bool wifiSetupIsActive();
void wifiSetupHandleEncoder(int16_t enc);
void wifiSetupHandleClick();
bool wifiSetupWantsExit();
void wifiSetupDraw();

#endif // WIFISETUP_H
