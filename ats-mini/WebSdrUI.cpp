#include "Common.h"
#include "Themes.h"
#include "Draw.h"
#include "WebSdrClient.h"
#include "WebSdrAudio.h"
#include "PwmAudio.h"

// ---------------------------------------------------------------------------
// WebSDR UI Screen — HAM Radio Style
//
// Compact layout with rotary-navigable menu buttons:
//   TUNE / BAND / MODE / EXIT
//
// Rotary navigation:
//   - Rotate to move between buttons (dark blue highlight)
//   - Click to enter edit mode (bright blue highlight)
//   - Rotate to change value
//   - Click again to exit edit mode
//   - EXIT button leaves WebSDR mode
// ---------------------------------------------------------------------------

// Color definitions (565 format)
#define COL_BTN_BG        0x18E3   // Dark gray button background
#define COL_BTN_HOVER     0x0014   // Dark blue — navigating over button
#define COL_BTN_ACTIVE    0x033F   // Bright blue — editing/selected
#define COL_BTN_BORDER    0x4A69   // Subtle gray border
#define COL_BTN_TEXT      0xC618   // Light gray label
#define COL_BTN_VALUE     0xFFFF   // White value text
#define COL_EXIT_ACTIVE   0xF800   // Red for EXIT when activated

// Menu items
enum WebSdrMenuItem {
  WSDR_ITEM_TUNE = 0,
  WSDR_ITEM_BAND,
  WSDR_ITEM_MODE,
  WSDR_ITEM_VOL,
  WSDR_ITEM_EXIT,
  WSDR_ITEM_COUNT
};

// Navigation state
static int  wsdrMenuIdx   = 0;
static bool wsdrEditing   = false;
static bool wsdrExitReq   = false;  // Set when EXIT is clicked
static int  wsdrVolLevel   = 8;     // Volume 0-10 (default 8 = ~200/255)

// ---------------------------------------------------------------------------
// Input handling
// ---------------------------------------------------------------------------

void webSdrHandleEncoder(int16_t enc)
{
  if (!enc) return;

  if (wsdrEditing)
  {
    switch (wsdrMenuIdx)
    {
      case WSDR_ITEM_TUNE:
        webSdrSetFrequency(webSdrGetFrequency() + enc);
        break;
      case WSDR_ITEM_BAND:
      {
        int band = webSdrGetBand() + enc;
        int count = webSdrGetBandCount();
        if (band < 0) band = count - 1;
        if (band >= count) band = 0;
        webSdrSetBand(band);
        break;
      }
      case WSDR_ITEM_MODE:
      {
        int mod = (int)webSdrGetModulation() + enc;
        if (mod < 0) mod = WEBSDR_MOD_COUNT - 1;
        if (mod >= WEBSDR_MOD_COUNT) mod = 0;
        webSdrSetModulation((WebSdrModulation)mod);
        break;
      }
      case WSDR_ITEM_VOL:
      {
        wsdrVolLevel += enc;
        if (wsdrVolLevel < 0)  wsdrVolLevel = 0;
        if (wsdrVolLevel > 10) wsdrVolLevel = 10;
        pwmAudioSetVolume(wsdrVolLevel * 255 / 10);
        break;
      }
      case WSDR_ITEM_EXIT:
        // No value to change on EXIT
        break;
    }
  }
  else
  {
    wsdrMenuIdx += (enc > 0) ? 1 : -1;
    if (wsdrMenuIdx < 0) wsdrMenuIdx = WSDR_ITEM_COUNT - 1;
    if (wsdrMenuIdx >= WSDR_ITEM_COUNT) wsdrMenuIdx = 0;
  }
}

bool webSdrHandleClick(void)
{
  if (wsdrMenuIdx == WSDR_ITEM_EXIT)
  {
    wsdrExitReq = true;
    return true;
  }
  wsdrEditing = !wsdrEditing;
  return true;
}

bool webSdrIsEditing(void)
{
  return wsdrEditing;
}

bool webSdrWantsExit(void)
{
  if (wsdrExitReq) { wsdrExitReq = false; return true; }
  return false;
}

// ---------------------------------------------------------------------------
// Compact button: state-driven colors, rounded corners
// ---------------------------------------------------------------------------

static void drawBtn(int x, int y, int w, int h,
                    const char *label, const char *value,
                    bool hovered, bool active)
{
  uint16_t bg, border, valCol;

  if (active)
  {
    bg     = COL_BTN_ACTIVE;
    border = COL_BTN_ACTIVE;
    valCol = COL_BTN_VALUE;
  }
  else if (hovered)
  {
    bg     = COL_BTN_HOVER;
    border = COL_BTN_HOVER;
    valCol = TFT_CYAN;
  }
  else
  {
    bg     = COL_BTN_BG;
    border = COL_BTN_BORDER;
    valCol = COL_BTN_TEXT;
  }

  spr.fillSmoothRoundRect(x, y, w, h, 5, bg);
  spr.drawSmoothRoundRect(x, y, 5, 4, w - 1, h - 1, border, TH.bg);

  // Label (small)
  spr.setTextDatum(TC_DATUM);
  spr.setTextFont(1);
  spr.setTextColor((active || hovered) ? TFT_WHITE : COL_BTN_TEXT, bg);
  spr.drawString(label, x + w / 2, y + 2);

  // Value (larger)
  spr.setTextFont(2);
  spr.setTextColor(valCol, bg);
  spr.drawString(value, x + w / 2, y + 12);
}

// Exit button (special red when hovered/active)
static void drawExitBtn(int x, int y, int w, int h,
                        bool hovered, bool active)
{
  uint16_t bg, border;

  if (active || hovered)
  {
    bg     = hovered ? 0x8000 : COL_EXIT_ACTIVE;  // dark red / red
    border = COL_EXIT_ACTIVE;
  }
  else
  {
    bg     = COL_BTN_BG;
    border = COL_BTN_BORDER;
  }

  spr.fillSmoothRoundRect(x, y, w, h, 5, bg);
  spr.drawSmoothRoundRect(x, y, 5, 4, w - 1, h - 1, border, TH.bg);

  spr.setTextDatum(MC_DATUM);
  spr.setTextFont(2);
  spr.setTextColor((active || hovered) ? TFT_WHITE : COL_BTN_TEXT, bg);
  spr.drawString("EXIT", x + w / 2, y + h / 2);
  spr.setTextDatum(TL_DATUM);
}

// ---------------------------------------------------------------------------
// Connection status dot + label
// ---------------------------------------------------------------------------

static void drawConnDot(int x, int y, WebSdrConnectionState cs)
{
  const char *str;
  uint16_t color;

  switch (cs)
  {
    case WEBSDR_STATE_STREAMING:      str = "LIVE";    color = TFT_GREEN;  break;
    case WEBSDR_STATE_CONNECTING:
    case WEBSDR_STATE_HANDSHAKE:      str = "CONN";    color = TFT_YELLOW; break;
    case WEBSDR_STATE_RECONNECT_WAIT: str = "RETRY";   color = TFT_ORANGE; break;
    case WEBSDR_STATE_ERROR:          str = "ERR";     color = TFT_RED;    break;
    default:                          str = "IDLE";    color = TH.text_muted; break;
  }

  spr.fillCircle(x, y + 4, 3, color);
  spr.setTextDatum(TL_DATUM);
  spr.setTextFont(1);
  spr.setTextColor(color, TH.bg);
  spr.drawString(str, x + 6, y);
}

// ---------------------------------------------------------------------------
// S-meter bar
// ---------------------------------------------------------------------------

static void drawSmeter(int x, int y, int w, int h)
{
  uint16_t smeter = webSdrAudioGetSmeter();
  int level = smeter / 41;
  if (level > 100) level = 100;
  int barW = (w - 2) * level / 100;

  spr.fillRect(x, y, w, h, TH.smeter_bar_empty);
  if (barW > 0)
  {
    uint16_t c = (level > 80) ? TH.smeter_bar_plus : TH.smeter_bar;
    spr.fillRect(x + 1, y + 1, barW, h - 2, c);
  }
  spr.drawRect(x, y, w, h, COL_BTN_BORDER);

  spr.setTextFont(1);
  spr.setTextColor(TH.scale_text, TH.bg);
  spr.setTextDatum(TL_DATUM);
  spr.drawString("S", x, y - 9);
}

// ---------------------------------------------------------------------------
// Main draw
// ---------------------------------------------------------------------------

void drawWebSdrScreen(void)
{
  const WebSdrState *st = webSdrGetState();

  // ---- TOP BAR ----
  const WebSdrServer *srv = webSdrGetServer(st->selectedServer);
  spr.setTextDatum(TL_DATUM);
  spr.setTextFont(1);
  spr.setTextColor(TH.text_muted, TH.bg);
  if (srv) spr.drawString(srv->name, 2, 2);

  drawConnDot(274, 1, st->connState);
  spr.drawFastHLine(0, 12, 320, COL_BTN_BORDER);

  // ---- FREQUENCY DISPLAY ----
  {
    uint32_t freq = st->currentFreq;  // 0.1 kHz units
    uint32_t mhz  = freq / 10000;
    uint32_t frac = freq % 10000;

    char freqBuf[16];
    snprintf(freqBuf, sizeof(freqBuf), "%lu.%04lu",
             (unsigned long)mhz, (unsigned long)frac);

    bool tuneEdit = wsdrEditing && wsdrMenuIdx == WSDR_ITEM_TUNE;
    bool tuneHL   = !wsdrEditing && wsdrMenuIdx == WSDR_ITEM_TUNE;

    // Highlight box around frequency (shorter width)
    if (tuneEdit)
    {
      spr.fillSmoothRoundRect(2, 14, 240, 38, 5, COL_BTN_ACTIVE);
    }
    else if (tuneHL)
    {
      spr.fillSmoothRoundRect(2, 14, 240, 38, 5, COL_BTN_HOVER);
    }

    uint16_t freqBg = (tuneEdit || tuneHL) ?
      (tuneEdit ? COL_BTN_ACTIVE : COL_BTN_HOVER) : TH.bg;

    spr.setFreeFont(&Orbitron_Light_24);
    spr.setTextDatum(TL_DATUM);
    spr.setTextColor(TFT_WHITE, freqBg);
    spr.drawString(freqBuf, 8, 19);

    spr.setFreeFont(NULL);
    spr.setTextFont(2);
    spr.setTextColor(TH.funit_text, freqBg);
    spr.drawString("MHz", 200, 26);

    // Band range
    const WebSdrBand *band = webSdrGetBandDef(st->currentBand);
    if (band)
    {
      char rng[24];
      snprintf(rng, sizeof(rng), "%lu-%lu",
               (unsigned long)band->minKHz, (unsigned long)band->maxKHz);
      spr.setTextDatum(TR_DATUM);
      spr.setTextFont(1);
      spr.setTextColor(TH.text_muted, TH.bg);
      spr.drawString(rng, 318, 40);
      spr.setTextDatum(TL_DATUM);
    }
  }

  // ---- S-METER ----
  drawSmeter(2, 56, 316, 8);

  // ---- BUTTON ROW: BAND | MODE | VOL | EXIT ----
  {
    const int btnY = 68, btnH = 28;
    const int gap = 4;
    // Four buttons evenly spaced
    const int btnW = (316 - 3 * gap) / 4;  // ~76px each

    // BAND
    const WebSdrBand *band = webSdrGetBandDef(st->currentBand);
    drawBtn(2, btnY, btnW, btnH, "BAND", band ? band->name : "?",
            !wsdrEditing && wsdrMenuIdx == WSDR_ITEM_BAND,
            wsdrEditing && wsdrMenuIdx == WSDR_ITEM_BAND);

    // MODE
    drawBtn(2 + (btnW + gap), btnY, btnW, btnH, "MODE",
            webSdrGetModulationName(st->currentMod),
            !wsdrEditing && wsdrMenuIdx == WSDR_ITEM_MODE,
            wsdrEditing && wsdrMenuIdx == WSDR_ITEM_MODE);

    // VOL
    {
      char volStr[8];
      snprintf(volStr, sizeof(volStr), "%d", wsdrVolLevel);
      drawBtn(2 + 2 * (btnW + gap), btnY, btnW, btnH, "VOL", volStr,
              !wsdrEditing && wsdrMenuIdx == WSDR_ITEM_VOL,
              wsdrEditing && wsdrMenuIdx == WSDR_ITEM_VOL);
    }

    // EXIT
    drawExitBtn(2 + 3 * (btnW + gap), btnY, btnW, btnH,
                !wsdrEditing && wsdrMenuIdx == WSDR_ITEM_EXIT,
                false);
  }

  // ---- STATUS LINE ----
  spr.drawFastHLine(0, 100, 320, COL_BTN_BORDER);

  spr.setTextFont(1);
  spr.setTextDatum(TL_DATUM);

  {
    char buf[64];
    snprintf(buf, sizeof(buf), "PWM %u  DEC %u  UND %lu  RX %lukB",
             (unsigned)pwmAudioGetBufferedBytes(),
             (unsigned)webSdrAudioAvailable(),
             (unsigned long)st->audioUnderruns,
             (unsigned long)(st->bytesReceived / 1024));
    spr.setTextColor(TH.text_muted, TH.bg);
    spr.drawString(buf, 4, 104);
  }

  // ---- HINT BAR ----
  spr.drawFastHLine(0, 116, 320, COL_BTN_BORDER);
  spr.setTextFont(1);
  spr.setTextColor(TH.text_muted, TH.bg);
  if (wsdrEditing)
  {
    const char *hints[] = { "Rotate: freq", "Rotate: band", "Rotate: mode", "Rotate: volume", "" };
    spr.drawString(hints[wsdrMenuIdx], 4, 120);
    spr.setTextDatum(TR_DATUM);
    spr.setTextColor(TFT_CYAN, TH.bg);
    spr.drawString("[PUSH: done]", 318, 120);
    spr.setTextDatum(TL_DATUM);
  }
  else
  {
    spr.drawString("Rotate: select   Push: edit/exit", 4, 120);
  }

  // ---- BATTERY ----
  drawBattery(BATT_OFFSET_X, 134);
}

void webSdrResetUI(void)
{
  wsdrMenuIdx   = WSDR_ITEM_TUNE;
  wsdrEditing   = false;
  wsdrExitReq   = false;
  wsdrVolLevel  = 8;
  pwmAudioSetVolume(wsdrVolLevel * 255 / 10);
}
