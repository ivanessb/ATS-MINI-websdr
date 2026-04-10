#include "Common.h"
#include "Storage.h"
#include "Themes.h"
#include "Draw.h"
#include "WiFiSetup.h"
#include "Menu.h"

#include <WiFi.h>

// -----------------------------------------------------------------------
// WiFi Setup — On-device WiFi network management
//
// Provides scan, select, password entry, and forget functionality
// using the rotary encoder and push button.
//
// States:
//   WIFISETUP_LIST     — Show saved networks + "Scan" + "Back"
//   WIFISETUP_SCANNING — WiFi scan in progress
//   WIFISETUP_RESULTS  — Show discovered networks
//   WIFISETUP_PASSWORD — Character-by-character password entry
// -----------------------------------------------------------------------

static bool     wsActive    = false;
static bool     wsExitReq   = false;
static uint8_t  wsState     = WIFISETUP_LIST;
static int8_t   wsCursor    = 0;

// Saved network slots (3 max, matching existing NVS layout)
#define MAX_SAVED 3

static String savedSSID[MAX_SAVED];
static String savedPass[MAX_SAVED];

// Scan results
static int      scanCount   = 0;
static String   scanSSID[WIFISETUP_MAX_SCAN];
static int32_t  scanRSSI[WIFISETUP_MAX_SCAN];
static bool     scanOpen[WIFISETUP_MAX_SCAN];  // true if no encryption

// Password entry
static String  pwSSID;               // SSID being configured
static char    pwBuffer[WIFISETUP_MAX_PASS + 1];
static uint8_t pwLen      = 0;
static uint8_t pwCharIdx  = 0;       // Index into character set
static bool    pwConfirm  = false;   // true when cursor is on OK/Back

// Character set for password entry (common chars only)
static const char pwChars[] =
  "abcdefghijklmnopqrstuvwxyz"
  "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
  "0123456789"
  " _.-@#$!+";

static const int pwCharsCount = sizeof(pwChars) - 1; // exclude null terminator

// Number of action items at end: [OK] [Back] [Backspace]
#define PW_ACTION_OK    (pwCharsCount)
#define PW_ACTION_BACK  (pwCharsCount + 1)
#define PW_ACTION_BKSP  (pwCharsCount + 2)
#define PW_TOTAL_ITEMS  (pwCharsCount + 3)

// -----------------------------------------------------------------------
// Load saved networks from NVS
// -----------------------------------------------------------------------
static void loadSavedNetworks()
{
  prefs.begin("network", true, STORAGE_PARTITION);
  for (int j = 0; j < MAX_SAVED; j++)
  {
    char nameSSID[16], namePASS[16];
    sprintf(nameSSID, "wifissid%d", j + 1);
    sprintf(namePASS, "wifipass%d", j + 1);
    savedSSID[j] = prefs.getString(nameSSID, "");
    savedPass[j] = prefs.getString(namePASS, "");
  }
  prefs.end();
}

// -----------------------------------------------------------------------
// Save a network to the first available slot (or overwrite if SSID exists)
// -----------------------------------------------------------------------
static bool saveNetwork(const String &ssid, const String &pass)
{
  if (ssid.length() == 0) return false;

  int slot = -1;

  // Check if SSID already exists
  for (int j = 0; j < MAX_SAVED; j++)
  {
    if (savedSSID[j] == ssid)
    {
      slot = j;
      break;
    }
  }

  // Find first empty slot
  if (slot < 0)
  {
    for (int j = 0; j < MAX_SAVED; j++)
    {
      if (savedSSID[j].length() == 0)
      {
        slot = j;
        break;
      }
    }
  }

  // No slot available
  if (slot < 0) return false;

  savedSSID[slot] = ssid;
  savedPass[slot] = pass;

  prefs.begin("network", false, STORAGE_PARTITION);
  char nameSSID[16], namePASS[16];
  sprintf(nameSSID, "wifissid%d", slot + 1);
  sprintf(namePASS, "wifipass%d", slot + 1);
  prefs.putString(nameSSID, ssid);
  prefs.putString(namePASS, pass);
  prefs.end();

  return true;
}

// -----------------------------------------------------------------------
// Forget a saved network by slot index
// -----------------------------------------------------------------------
static void forgetNetwork(int slot)
{
  if (slot < 0 || slot >= MAX_SAVED) return;

  savedSSID[slot] = "";
  savedPass[slot] = "";

  prefs.begin("network", false, STORAGE_PARTITION);
  char nameSSID[16], namePASS[16];
  sprintf(nameSSID, "wifissid%d", slot + 1);
  sprintf(namePASS, "wifipass%d", slot + 1);
  prefs.putString(nameSSID, "");
  prefs.putString(namePASS, "");
  prefs.end();
}

// -----------------------------------------------------------------------
// Enter / exit WiFi setup mode
// -----------------------------------------------------------------------
void wifiSetupEnter()
{
  wsActive  = true;
  wsExitReq = false;
  wsState   = WIFISETUP_LIST;
  wsCursor  = 0;
  loadSavedNetworks();
}

void wifiSetupExit()
{
  wsActive  = false;
  wsExitReq = false;
  wsState   = WIFISETUP_LIST;
  // Clean up any ongoing scan
  WiFi.scanDelete();
}

bool wifiSetupIsActive()
{
  return wsActive;
}

bool wifiSetupWantsExit()
{
  if (wsExitReq)
  {
    wsExitReq = false;
    return true;
  }
  return false;
}

// -----------------------------------------------------------------------
// Start WiFi scan
// -----------------------------------------------------------------------
static void startScan()
{
  wsState   = WIFISETUP_SCANNING;
  wsCursor  = 0;
  scanCount = 0;

  // Enable WiFi radio for scanning if it's off
  if (WiFi.getMode() == WIFI_MODE_NULL)
    WiFi.mode(WIFI_STA);

  // Async scan — results polled in draw
  WiFi.scanNetworks(true);
}

// -----------------------------------------------------------------------
// Collect scan results
// -----------------------------------------------------------------------
static bool collectScanResults()
{
  int16_t n = WiFi.scanComplete();

  if (n == WIFI_SCAN_RUNNING) return false;  // still scanning

  if (n == WIFI_SCAN_FAILED || n <= 0)
  {
    scanCount = 0;
    wsState   = WIFISETUP_RESULTS;
    return true;
  }

  scanCount = n > WIFISETUP_MAX_SCAN ? WIFISETUP_MAX_SCAN : n;
  for (int i = 0; i < scanCount; i++)
  {
    scanSSID[i] = WiFi.SSID(i);
    scanRSSI[i] = WiFi.RSSI(i);
    scanOpen[i] = (WiFi.encryptionType(i) == WIFI_AUTH_OPEN);
  }

  WiFi.scanDelete();
  wsState  = WIFISETUP_RESULTS;
  wsCursor = 0;
  return true;
}

// -----------------------------------------------------------------------
// Enter password entry for a given SSID
// -----------------------------------------------------------------------
static void enterPassword(const String &ssid)
{
  pwSSID    = ssid;
  pwLen     = 0;
  pwBuffer[0] = '\0';
  pwCharIdx = 0;
  pwConfirm = false;
  wsState   = WIFISETUP_PASSWORD;
}

// -----------------------------------------------------------------------
// Encoder handling
// -----------------------------------------------------------------------
void wifiSetupHandleEncoder(int16_t enc)
{
  if (!enc) return;

  // Clamp to ±1 so fast rotation doesn't skip over buttons
  enc = (enc > 0) ? 1 : -1;

  switch (wsState)
  {
    case WIFISETUP_LIST:
    {
      // Items: saved[0..MAX_SAVED-1] (only non-empty shown), "Scan", "Back"
      int totalItems = 0;
      for (int j = 0; j < MAX_SAVED; j++)
        if (savedSSID[j].length() > 0) totalItems++;
      totalItems += 2; // Scan + Back

      wsCursor += enc;
      if (wsCursor < 0) wsCursor = totalItems - 1;
      if (wsCursor >= totalItems) wsCursor = 0;
      break;
    }

    case WIFISETUP_RESULTS:
    {
      int totalItems = scanCount + 1; // scan results + Back
      wsCursor += enc;
      if (wsCursor < 0) wsCursor = totalItems - 1;
      if (wsCursor >= totalItems) wsCursor = 0;
      break;
    }

    case WIFISETUP_PASSWORD:
    {
      // Scroll through characters + action buttons
      pwCharIdx = ((int)pwCharIdx + enc);
      // Wrap around within total items
      int total = PW_TOTAL_ITEMS;
      pwCharIdx = ((int)pwCharIdx % total + total) % total;
      break;
    }

    default:
      break;
  }
}

// -----------------------------------------------------------------------
// Click handling
// -----------------------------------------------------------------------
void wifiSetupHandleClick()
{
  switch (wsState)
  {
    case WIFISETUP_LIST:
    {
      // Build item list: saved networks (non-empty), then "Scan", then "Back"
      int idx = 0;
      for (int j = 0; j < MAX_SAVED; j++)
      {
        if (savedSSID[j].length() == 0) continue;
        if (wsCursor == idx)
        {
          // Clicking a saved network — forget it
          forgetNetwork(j);
          loadSavedNetworks();
          wsCursor = 0;
          return;
        }
        idx++;
      }

      if (wsCursor == idx)
      {
        // "Scan for Networks"
        startScan();
      }
      else
      {
        // "Back"
        wsExitReq = true;
      }
      break;
    }

    case WIFISETUP_RESULTS:
    {
      if (wsCursor < scanCount)
      {
        // Selected a scanned network
        if (scanOpen[wsCursor])
        {
          // Open network — save with empty password and go back
          saveNetwork(scanSSID[wsCursor], "");
          loadSavedNetworks();
          wsState  = WIFISETUP_LIST;
          wsCursor = 0;
        }
        else
        {
          // Encrypted — enter password
          enterPassword(scanSSID[wsCursor]);
        }
      }
      else
      {
        // "Back"
        wsState  = WIFISETUP_LIST;
        wsCursor = 0;
      }
      break;
    }

    case WIFISETUP_PASSWORD:
    {
      if (pwCharIdx < (uint8_t)pwCharsCount)
      {
        // Append character
        if (pwLen < WIFISETUP_MAX_PASS)
        {
          pwBuffer[pwLen++] = pwChars[pwCharIdx];
          pwBuffer[pwLen]   = '\0';
        }
      }
      else if (pwCharIdx == PW_ACTION_OK)
      {
        // Save and go back to list
        saveNetwork(pwSSID, String(pwBuffer));
        loadSavedNetworks();
        wsState  = WIFISETUP_LIST;
        wsCursor = 0;
      }
      else if (pwCharIdx == PW_ACTION_BACK)
      {
        // Cancel — back to scan results
        wsState  = WIFISETUP_RESULTS;
        wsCursor = 0;
      }
      else if (pwCharIdx == PW_ACTION_BKSP)
      {
        // Backspace
        if (pwLen > 0)
        {
          pwLen--;
          pwBuffer[pwLen] = '\0';
        }
      }
      break;
    }

    default:
      break;
  }
}

// -----------------------------------------------------------------------
// Drawing helpers
// -----------------------------------------------------------------------

static void drawTitle(const char *title)
{
  spr.setTextDatum(TC_DATUM);
  spr.setTextColor(TH.menu_hdr, TH.bg);
  spr.drawString(title, 160, 2, 2);
  spr.drawLine(0, 18, 319, 18, TH.menu_border);
}

static void drawListItem(int y, const char *text, bool selected, const char *right = nullptr)
{
  if (selected)
  {
    spr.fillRoundRect(4, y, 312, 18, 3, TH.menu_hl_bg);
    spr.setTextColor(TH.menu_hl_text, TH.menu_hl_bg);
  }
  else
  {
    spr.setTextColor(TH.menu_item, TH.bg);
  }

  spr.setTextDatum(ML_DATUM);
  spr.drawString(text, 10, y + 9, 2);

  if (right)
  {
    spr.setTextDatum(MR_DATUM);
    if (selected)
      spr.setTextColor(TH.menu_hl_text, TH.menu_hl_bg);
    else
      spr.setTextColor(TH.menu_param, TH.bg);
    spr.drawString(right, 310, y + 9, 2);
  }
}

// Signal strength bars icon
static void drawSignalBars(int x, int y, int32_t rssiVal, bool selected)
{
  int bars;
  if (rssiVal > -50)       bars = 4;
  else if (rssiVal > -60)  bars = 3;
  else if (rssiVal > -70)  bars = 2;
  else if (rssiVal > -80)  bars = 1;
  else                     bars = 0;

  uint16_t color = selected ? TH.menu_hl_text : TH.menu_param;
  uint16_t dimColor = selected ? TH.menu_hl_bg : TH.bg;

  for (int i = 0; i < 4; i++)
  {
    int bh = 3 + i * 3;
    int bx = x + i * 5;
    int by = y + 12 - bh;
    if (i < bars)
      spr.fillRect(bx, by, 4, bh, color);
    else
      spr.fillRect(bx, by, 4, bh, dimColor);
  }
}

// -----------------------------------------------------------------------
// Draw WiFi Setup screen
// -----------------------------------------------------------------------
void wifiSetupDraw()
{
  switch (wsState)
  {
    // ----- Saved Networks List -----
    case WIFISETUP_LIST:
    {
      drawTitle("WiFi Setup");

      int y   = 22;
      int idx = 0;

      // Saved networks
      for (int j = 0; j < MAX_SAVED; j++)
      {
        if (savedSSID[j].length() == 0) continue;

        bool connected = (WiFi.status() == WL_CONNECTED && WiFi.SSID() == savedSSID[j]);
        const char *status = connected ? "Connected" : "Forget";

        drawListItem(y, savedSSID[j].c_str(), wsCursor == idx, status);
        y += 20;
        idx++;
      }

      // Separator
      if (idx > 0)
      {
        spr.drawLine(10, y + 2, 310, y + 2, TH.menu_border);
        y += 6;
      }

      // "Scan for Networks"
      drawListItem(y, "Scan for Networks", wsCursor == idx);
      y += 20;
      idx++;

      // "Back"
      drawListItem(y, "Back", wsCursor == idx);

      // Hint at bottom
      spr.setTextDatum(BC_DATUM);
      spr.setTextColor(TH.menu_item, TH.bg);
      spr.drawString("Click saved network to forget", 160, 166, 1);
      break;
    }

    // ----- Scanning -----
    case WIFISETUP_SCANNING:
    {
      drawTitle("WiFi Setup");

      // Check if scan is done
      collectScanResults();

      if (wsState == WIFISETUP_SCANNING)
      {
        // Still scanning — show animation
        spr.setTextDatum(MC_DATUM);
        spr.setTextColor(TH.menu_param, TH.bg);
        spr.drawString("Scanning...", 160, 85, 4);
      }
      // else: state changed to WIFISETUP_RESULTS, will be drawn next frame
      break;
    }

    // ----- Scan Results -----
    case WIFISETUP_RESULTS:
    {
      drawTitle("Select Network");

      if (scanCount == 0)
      {
        spr.setTextDatum(MC_DATUM);
        spr.setTextColor(TH.menu_item, TH.bg);
        spr.drawString("No networks found", 160, 70, 2);

        drawListItem(100, "Back", wsCursor == 0);
        break;
      }

      // Show up to 6 visible items at a time (scrolling window)
      int maxVisible = 6;
      int startIdx   = 0;
      int totalItems = scanCount + 1; // networks + Back

      if (wsCursor > maxVisible - 2 && totalItems > maxVisible)
      {
        startIdx = wsCursor - (maxVisible - 2);
        if (startIdx + maxVisible > totalItems)
          startIdx = totalItems - maxVisible;
        if (startIdx < 0) startIdx = 0;
      }

      int y = 22;
      for (int i = startIdx; i < startIdx + maxVisible && i < totalItems; i++)
      {
        if (i < scanCount)
        {
          // Network entry
          char rssiStr[8];
          sprintf(rssiStr, "%ddBm", (int)scanRSSI[i]);

          bool sel = (wsCursor == i);
          String label = scanSSID[i];
          if (scanOpen[i]) label += " (Open)";

          drawListItem(y, label.c_str(), sel);

          // Draw signal bars
          drawSignalBars(280, y + 3, scanRSSI[i], sel);
        }
        else
        {
          // "Back" item
          drawListItem(y, "Back", wsCursor == i);
        }
        y += 20;
      }

      // Scroll indicators
      if (startIdx > 0)
      {
        spr.setTextDatum(TR_DATUM);
        spr.setTextColor(TH.menu_item, TH.bg);
        spr.drawString("\x18", 316, 22, 2); // up arrow
      }
      if (startIdx + maxVisible < totalItems)
      {
        spr.setTextDatum(BR_DATUM);
        spr.setTextColor(TH.menu_item, TH.bg);
        spr.drawString("\x19", 316, 22 + maxVisible * 20, 2); // down arrow
      }
      break;
    }

    // ----- Password Entry -----
    case WIFISETUP_PASSWORD:
    {
      drawTitle("Enter Password");

      // Network name
      spr.setTextDatum(TL_DATUM);
      spr.setTextColor(TH.menu_param, TH.bg);
      spr.drawString(pwSSID.c_str(), 10, 22, 2);

      // Password field with mask
      spr.setTextDatum(TL_DATUM);
      spr.setTextColor(TH.menu_item, TH.bg);
      spr.drawString("Password:", 10, 42, 2);

      // Show last 20 chars of password (masked with * except last char)
      char display[22];
      int showStart = pwLen > 20 ? pwLen - 20 : 0;
      int showLen   = pwLen - showStart;
      for (int i = 0; i < showLen; i++)
      {
        if (i == showLen - 1)
          display[i] = pwBuffer[showStart + i]; // Show last char
        else
          display[i] = '*';
      }
      display[showLen] = '_'; // cursor
      display[showLen + 1] = '\0';

      spr.setTextColor(TH.menu_hl_text, TH.bg);
      spr.drawString(display, 80, 42, 2);

      // Character length indicator
      {
        char lenStr[8];
        sprintf(lenStr, "%d", pwLen);
        spr.setTextDatum(TR_DATUM);
        spr.setTextColor(TH.menu_item, TH.bg);
        spr.drawString(lenStr, 310, 42, 2);
      }

      // Current character selector
      spr.drawLine(0, 62, 319, 62, TH.menu_border);

      // Show a window of characters around current selection
      int charWindowSize = 15;
      int charY = 68;

      spr.setTextDatum(MC_DATUM);

      // Draw character row
      for (int i = -charWindowSize / 2; i <= charWindowSize / 2; i++)
      {
        int ci = ((int)pwCharIdx + i);
        int total = PW_TOTAL_ITEMS;
        ci = ((ci % total) + total) % total;

        int xPos = 160 + i * 20;

        if (xPos < 0 || xPos > 320) continue;

        char ch[4];
        const char *label = ch;

        if (ci < pwCharsCount)
        {
          ch[0] = pwChars[ci];
          ch[1] = '\0';
        }
        else if (ci == PW_ACTION_OK)
          label = "OK";
        else if (ci == PW_ACTION_BACK)
          label = "\x1b"; // left arrow = back
        else if (ci == PW_ACTION_BKSP)
          label = "\x11"; // backspace symbol

        if (i == 0)
        {
          // Highlighted character
          spr.fillRoundRect(xPos - 10, charY - 10, 20, 20, 3, TH.menu_hl_bg);
          spr.setTextColor(TH.menu_hl_text, TH.menu_hl_bg);
          spr.drawString(label, xPos, charY, 2);
        }
        else
        {
          spr.setTextColor(TH.menu_item, TH.bg);
          spr.drawString(label, xPos, charY, 2);
        }
      }

      // Draw action buttons at bottom
      int btnY   = 100;
      int btnW   = 80;
      int btnH   = 24;
      int btnGap = 10;

      // [Backspace] button
      {
        bool sel = (pwCharIdx == PW_ACTION_BKSP);
        uint16_t bg = sel ? TH.menu_hl_bg : TH.menu_border;
        uint16_t tc = sel ? TH.menu_hl_text : TH.menu_item;
        spr.fillRoundRect(20, btnY, btnW, btnH, 4, bg);
        spr.setTextDatum(MC_DATUM);
        spr.setTextColor(tc, bg);
        spr.drawString("Delete", 20 + btnW / 2, btnY + btnH / 2, 2);
      }

      // [OK] button
      {
        bool sel = (pwCharIdx == PW_ACTION_OK);
        uint16_t bg = sel ? TH.menu_hl_bg : TH.menu_border;
        uint16_t tc = sel ? TH.menu_hl_text : TH.menu_item;
        spr.fillRoundRect(120, btnY, btnW, btnH, 4, bg);
        spr.setTextDatum(MC_DATUM);
        spr.setTextColor(tc, bg);
        spr.drawString("Save", 120 + btnW / 2, btnY + btnH / 2, 2);
      }

      // [Back] button
      {
        bool sel = (pwCharIdx == PW_ACTION_BACK);
        uint16_t bg = sel ? TH.menu_hl_bg : TH.menu_border;
        uint16_t tc = sel ? TH.menu_hl_text : TH.menu_item;
        spr.fillRoundRect(220, btnY, btnW, btnH, 4, bg);
        spr.setTextDatum(MC_DATUM);
        spr.setTextColor(tc, bg);
        spr.drawString("Cancel", 220 + btnW / 2, btnY + btnH / 2, 2);
      }

      // Hint
      spr.setTextDatum(BC_DATUM);
      spr.setTextColor(TH.menu_item, TH.bg);
      spr.drawString("Rotate=char  Click=add  Buttons=action", 160, 166, 1);
      break;
    }
  }
}
