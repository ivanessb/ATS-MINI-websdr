#include "LvglUI.h"
#include "Common.h"
#include "Menu.h"
#include "Themes.h"
#include "Utils.h"
#include <TFT_eSPI.h>

// LVGL draw buffer (allocated in PSRAM)
static uint8_t *lvglBuf1 = NULL;
static uint8_t *lvglBuf2 = NULL;
#define LVGL_BUF_LINES 34
#define LVGL_BUF_SIZE  (320 * LVGL_BUF_LINES * 2)

static lv_display_t *lvDisplay = NULL;
static bool _lvglActive = false;

// Cached state for change detection
static uint16_t prevFrequency = 0;
static int16_t  prevBFO = 0;
static uint8_t  prevMode = 0xFF;
static uint8_t  prevRSSI = 0xFF;
static uint8_t  prevSNR = 0xFF;
static int      prevBandIdx = -1;

// LVGL widgets
static lv_obj_t *scrMain = NULL;
static lv_obj_t *scaleMeter = NULL;
static lv_obj_t *needleBase = NULL;
static lv_obj_t *needleTip = NULL;
static lv_obj_t *lblMode = NULL;
static lv_obj_t *lblBand = NULL;
static lv_obj_t *lblFrequency = NULL;
static lv_obj_t *lblUnit = NULL;
static lv_obj_t *lblStation = NULL;
static lv_obj_t *lblStepVal = NULL;
static lv_obj_t *lblBwVal = NULL;
static lv_obj_t *lblAgcVal = NULL;
static lv_obj_t *lblVolVal = NULL;
static lv_obj_t *lblSnrVal = NULL;

// Scale section styles (must be static/persistent)
static lv_style_t styleRedItems;
static lv_style_t styleRedIndicator;

// --- Display flush callback ---

static void lvglFlushCb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
  uint32_t w = lv_area_get_width(area);
  uint32_t h = lv_area_get_height(area);

  tft.startWrite();
  tft.setAddrWindow(area->x1, area->y1, w, h);
  tft.pushColors((uint16_t *)px_map, w * h, true);
  tft.endWrite();

  lv_display_flush_ready(disp);
}

// --- Format frequency like Icom: "14.195.500" ---

static void formatFrequency(char *buf, size_t bufSize, char *unitBuf, size_t unitSize)
{
  if(currentMode == FM)
  {
    int mhz = currentFrequency / 100;
    int khz10 = currentFrequency % 100;
    snprintf(buf, bufSize, "%d.%02d0", mhz, khz10);
    snprintf(unitBuf, unitSize, "MHz");
  }
  else if(isSSB())
  {
    int32_t totalHz = (int32_t)currentFrequency * 1000 + currentBFO;
    int32_t totalKhz = totalHz / 1000;
    int32_t hz = abs(totalHz % 1000);
    if(abs(totalKhz) >= 1000)
    {
      int32_t mhz = totalKhz / 1000;
      int32_t khz = abs(totalKhz) % 1000;
      snprintf(buf, bufSize, "%ld.%03ld.%03ld", (long)mhz, (long)khz, (long)hz);
    }
    else
    {
      snprintf(buf, bufSize, "%ld.%03ld", (long)totalKhz, (long)hz);
    }
    snprintf(unitBuf, unitSize, "kHz");
  }
  else
  {
    if(currentFrequency >= 1000)
      snprintf(buf, bufSize, "%d.%03d", currentFrequency / 1000, currentFrequency % 1000);
    else
      snprintf(buf, bufSize, "%d", currentFrequency);
    snprintf(unitBuf, unitSize, "kHz");
  }
}

// --- Create info box with header + value ---

static lv_obj_t *createInfoBox(lv_obj_t *parent, const char *title, lv_obj_t **valueLabel)
{
  lv_obj_t *box = lv_obj_create(parent);
  lv_obj_set_size(box, LV_SIZE_CONTENT, 58);
  lv_obj_set_flex_grow(box, 1);
  lv_obj_set_style_bg_color(box, lv_color_hex(0x141414), 0);
  lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(box, 1, 0);
  lv_obj_set_style_border_color(box, lv_color_hex(0x383838), 0);
  lv_obj_set_style_radius(box, 3, 0);
  lv_obj_set_style_pad_all(box, 3, 0);
  lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *lblTitle = lv_label_create(box);
  lv_obj_set_style_text_font(lblTitle, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lblTitle, lv_color_hex(0x777777), 0);
  lv_obj_align(lblTitle, LV_ALIGN_TOP_MID, 0, 0);
  lv_label_set_text(lblTitle, title);

  *valueLabel = lv_label_create(box);
  lv_obj_set_style_text_font(*valueLabel, &lv_font_montserrat_14, 0);
  lv_obj_set_style_text_color(*valueLabel, lv_color_white(), 0);
  lv_obj_align(*valueLabel, LV_ALIGN_BOTTOM_MID, 0, 0);
  lv_label_set_text(*valueLabel, "---");

  return box;
}

// --- Create the main screen ---

static void createMainScreen()
{
  scrMain = lv_obj_create(NULL);
  lv_obj_set_style_bg_color(scrMain, lv_color_black(), 0);
  lv_obj_set_style_bg_opa(scrMain, LV_OPA_COVER, 0);
  lv_obj_set_style_pad_all(scrMain, 0, 0);
  lv_obj_clear_flag(scrMain, LV_OBJ_FLAG_SCROLLABLE);

  // ================================================================
  //  LEFT: Analog S-Meter — wide and shallow
  // ================================================================

  lv_obj_t *meterBox = lv_obj_create(scrMain);
  lv_obj_set_size(meterBox, 210, 62);
  lv_obj_set_pos(meterBox, 2, 2);
  lv_obj_set_style_bg_color(meterBox, lv_color_hex(0x080808), 0);
  lv_obj_set_style_bg_opa(meterBox, LV_OPA_COVER, 0);
  lv_obj_set_style_border_width(meterBox, 1, 0);
  lv_obj_set_style_border_color(meterBox, lv_color_hex(0x2a2a2a), 0);
  lv_obj_set_style_radius(meterBox, 4, 0);
  lv_obj_set_style_pad_all(meterBox, 0, 0);
  lv_obj_set_style_clip_corner(meterBox, true, 0);
  lv_obj_clear_flag(meterBox, LV_OBJ_FLAG_SCROLLABLE);

  // Scale widget — large circle for flat arc, clipped by meterBox
  scaleMeter = lv_scale_create(meterBox);
  lv_obj_set_size(scaleMeter, 300, 300);
  lv_obj_align(scaleMeter, LV_ALIGN_CENTER, 0, 118);
  lv_scale_set_mode(scaleMeter, LV_SCALE_MODE_ROUND_INNER);
  lv_scale_set_range(scaleMeter, 0, 80);
  lv_scale_set_angle_range(scaleMeter, 130);
  lv_scale_set_rotation(scaleMeter, 205);
  lv_scale_set_total_tick_count(scaleMeter, 17);
  lv_scale_set_major_tick_every(scaleMeter, 2);
  lv_scale_set_label_show(scaleMeter, true);
  lv_scale_set_draw_ticks_on_top(scaleMeter, true);

  // Custom S-unit labels (9 major ticks)
  static const char *meterLabels[] = {
    "1", "3", "5", "7", "9", "+20", "+40", "+60", "", NULL
  };
  lv_scale_set_text_src(scaleMeter, meterLabels);

  // Main arc line
  lv_obj_set_style_arc_color(scaleMeter, lv_color_hex(0x444444), LV_PART_MAIN);
  lv_obj_set_style_arc_width(scaleMeter, 2, LV_PART_MAIN);

  // Minor ticks (thin, gray)
  lv_obj_set_style_length(scaleMeter, 5, LV_PART_INDICATOR);
  lv_obj_set_style_line_color(scaleMeter, lv_color_hex(0x555555), LV_PART_INDICATOR);
  lv_obj_set_style_line_width(scaleMeter, 1, LV_PART_INDICATOR);

  // Major ticks + labels (white, tiny font to prevent overlap)
  lv_obj_set_style_length(scaleMeter, 10, LV_PART_ITEMS);
  lv_obj_set_style_line_color(scaleMeter, lv_color_white(), LV_PART_ITEMS);
  lv_obj_set_style_line_width(scaleMeter, 2, LV_PART_ITEMS);
  lv_obj_set_style_text_color(scaleMeter, lv_color_white(), LV_PART_ITEMS);
  lv_obj_set_style_text_font(scaleMeter, &lv_font_montserrat_8, LV_PART_ITEMS);

  // Red section for +dB range (values 50-80 = S9+ region)
  lv_style_init(&styleRedItems);
  lv_style_set_line_color(&styleRedItems, lv_color_hex(0xFF3333));
  lv_style_set_text_color(&styleRedItems, lv_color_hex(0xFF3333));

  lv_style_init(&styleRedIndicator);
  lv_style_set_line_color(&styleRedIndicator, lv_color_hex(0xCC2222));

  lv_scale_section_t *redSection = lv_scale_add_section(scaleMeter);
  lv_scale_section_set_range(redSection, 50, 80);
  lv_scale_section_set_style(redSection, LV_PART_ITEMS, &styleRedItems);
  lv_scale_section_set_style(redSection, LV_PART_INDICATOR, &styleRedIndicator);

  // Tapered needle: thick base + thin tip
  needleBase = lv_line_create(scaleMeter);
  lv_obj_set_style_line_color(needleBase, lv_color_hex(0xFF4444), 0);
  lv_obj_set_style_line_width(needleBase, 4, 0);
  lv_scale_set_line_needle_value(scaleMeter, needleBase, 60, 0);

  needleTip = lv_line_create(scaleMeter);
  lv_obj_set_style_line_color(needleTip, lv_color_hex(0xFF4444), 0);
  lv_obj_set_style_line_width(needleTip, 1, 0);
  lv_scale_set_line_needle_value(scaleMeter, needleTip, 100, 0);

  // "S" label at bottom-left of meter
  lv_obj_t *lblS = lv_label_create(meterBox);
  lv_obj_set_style_text_font(lblS, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lblS, lv_color_hex(0x999999), 0);
  lv_obj_align(lblS, LV_ALIGN_BOTTOM_MID, 0, -1);
  lv_label_set_text(lblS, "S");

  // ================================================================
  //  RIGHT: Mode, Frequency, Band, Station
  // ================================================================

  // Mode badge (USB/LSB/AM/FM)
  lblMode = lv_label_create(scrMain);
  lv_obj_set_style_text_font(lblMode, &lv_font_montserrat_14, 0);
  lv_obj_set_style_text_color(lblMode, lv_color_black(), 0);
  lv_obj_set_style_bg_color(lblMode, lv_color_hex(0xFF3333), 0);
  lv_obj_set_style_bg_opa(lblMode, LV_OPA_COVER, 0);
  lv_obj_set_style_pad_hor(lblMode, 8, 0);
  lv_obj_set_style_pad_ver(lblMode, 2, 0);
  lv_obj_set_style_radius(lblMode, 3, 0);
  lv_obj_set_pos(lblMode, 218, 3);
  lv_label_set_text(lblMode, "FM");

  // Band label
  lblBand = lv_label_create(scrMain);
  lv_obj_set_style_text_font(lblBand, &lv_font_montserrat_14, 0);
  lv_obj_set_style_text_color(lblBand, lv_color_hex(0x00CC66), 0);
  lv_obj_set_pos(lblBand, 270, 4);
  lv_label_set_text(lblBand, "");

  // Large frequency display
  lblFrequency = lv_label_create(scrMain);
  lv_obj_set_style_text_font(lblFrequency, &lv_font_montserrat_20, 0);
  lv_obj_set_style_text_color(lblFrequency, lv_color_white(), 0);
  lv_obj_set_pos(lblFrequency, 218, 22);
  lv_label_set_text(lblFrequency, "0.000");

  // Unit label (kHz/MHz)
  lblUnit = lv_label_create(scrMain);
  lv_obj_set_style_text_font(lblUnit, &lv_font_montserrat_10, 0);
  lv_obj_set_style_text_color(lblUnit, lv_color_hex(0x888888), 0);
  lv_obj_set_pos(lblUnit, 218, 46);
  lv_label_set_text(lblUnit, "kHz");

  // Station name (RDS / EIBI)
  lblStation = lv_label_create(scrMain);
  lv_obj_set_style_text_font(lblStation, &lv_font_montserrat_12, 0);
  lv_obj_set_style_text_color(lblStation, lv_color_hex(0x44AAFF), 0);
  lv_obj_set_width(lblStation, 100);
  lv_label_set_long_mode(lblStation, LV_LABEL_LONG_CLIP);
  lv_obj_set_pos(lblStation, 218, 56);
  lv_label_set_text(lblStation, "");

  // ================================================================
  //  SEPARATOR
  // ================================================================

  static lv_point_precise_t sepPts[] = {{0, 0}, {316, 0}};
  lv_obj_t *sep = lv_line_create(scrMain);
  lv_line_set_points(sep, sepPts, 2);
  lv_obj_set_style_line_color(sep, lv_color_hex(0x333333), 0);
  lv_obj_set_style_line_width(sep, 1, 0);
  lv_obj_set_pos(sep, 2, 68);

  // ================================================================
  //  BOTTOM: Info boxes row
  // ================================================================

  lv_obj_t *infoRow = lv_obj_create(scrMain);
  lv_obj_set_size(infoRow, 316, 96);
  lv_obj_set_pos(infoRow, 2, 72);
  lv_obj_set_style_bg_opa(infoRow, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(infoRow, 0, 0);
  lv_obj_set_style_pad_all(infoRow, 0, 0);
  lv_obj_set_style_pad_column(infoRow, 4, 0);
  lv_obj_set_layout(infoRow, LV_LAYOUT_FLEX);
  lv_obj_set_flex_flow(infoRow, LV_FLEX_FLOW_ROW);
  lv_obj_set_flex_align(infoRow, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
  lv_obj_clear_flag(infoRow, LV_OBJ_FLAG_SCROLLABLE);

  createInfoBox(infoRow, "STEP",  &lblStepVal);
  createInfoBox(infoRow, "BW",    &lblBwVal);
  createInfoBox(infoRow, "AGC",   &lblAgcVal);
  createInfoBox(infoRow, "VOL",   &lblVolVal);
  createInfoBox(infoRow, "SNR",   &lblSnrVal);
}

// --- Public API ---

void lvglInit()
{
  lv_init();

  lvglBuf1 = (uint8_t *)ps_malloc(LVGL_BUF_SIZE);
  lvglBuf2 = (uint8_t *)ps_malloc(LVGL_BUF_SIZE);

  if(!lvglBuf1 || !lvglBuf2)
  {
    Serial.println("LVGL: Failed to allocate draw buffers");
    return;
  }

  lvDisplay = lv_display_create(320, 170);
  lv_display_set_flush_cb(lvDisplay, lvglFlushCb);
  lv_display_set_buffers(lvDisplay, lvglBuf1, lvglBuf2, LVGL_BUF_SIZE, LV_DISPLAY_RENDER_MODE_PARTIAL);
  lv_display_set_color_format(lvDisplay, LV_COLOR_FORMAT_RGB565);

  lv_theme_t *th = lv_theme_default_init(
    lvDisplay,
    lv_palette_main(LV_PALETTE_CYAN),
    lv_palette_main(LV_PALETTE_CYAN),
    true,
    &lv_font_montserrat_14
  );
  lv_display_set_theme(lvDisplay, th);

  createMainScreen();

  lv_tick_set_cb([]() -> uint32_t { return millis(); });

  _lvglActive = true;
  prevFrequency = 0xFFFF;
  prevBFO = 0x7FFF;
  prevMode = 0xFF;
  prevRSSI = 0xFF;
  prevBandIdx = -1;

  lv_screen_load(scrMain);
}

void lvglUpdate()
{
  if(!_lvglActive || !scrMain) return;

  // Update mode badge
  if(currentMode != prevMode)
  {
    prevMode = currentMode;
    const char *modeNames[] = {"FM", "LSB", "USB", "AM"};
    lv_label_set_text(lblMode, modeNames[currentMode]);

    // Color by mode
    if(currentMode == USB || currentMode == LSB)
      lv_obj_set_style_bg_color(lblMode, lv_color_hex(0xFF3333), 0);
    else if(currentMode == FM)
      lv_obj_set_style_bg_color(lblMode, lv_color_hex(0x00AA44), 0);
    else
      lv_obj_set_style_bg_color(lblMode, lv_color_hex(0x0088DD), 0);
  }

  // Update band
  if(bandIdx != prevBandIdx)
  {
    prevBandIdx = bandIdx;
    lv_label_set_text(lblBand, bands[bandIdx].bandName);
  }

  // Update frequency
  if(currentFrequency != prevFrequency || currentBFO != prevBFO)
  {
    prevFrequency = currentFrequency;
    prevBFO = currentBFO;

    char freqBuf[32], unitBuf[8];
    formatFrequency(freqBuf, sizeof(freqBuf), unitBuf, sizeof(unitBuf));
    lv_label_set_text(lblFrequency, freqBuf);
    lv_label_set_text(lblUnit, unitBuf);
  }

  // Update station name
  const char *station = getStationName();
  lv_label_set_text(lblStation, (station && *station) ? station : "");

  // Update analog meter needle + SNR
  if(rssi != prevRSSI || snr != prevSNR)
  {
    prevRSSI = rssi;
    prevSNR = snr;

    int32_t meterVal = rssi > 80 ? 80 : rssi;
    lv_scale_set_line_needle_value(scaleMeter, needleBase, 60, meterVal);
    lv_scale_set_line_needle_value(scaleMeter, needleTip, 100, meterVal);

    char buf[8];
    snprintf(buf, sizeof(buf), "%d", snr);
    lv_label_set_text(lblSnrVal, buf);
  }

  // Update step
  const Step *step = getCurrentStep();
  if(step) lv_label_set_text(lblStepVal, step->desc);

  // Update bandwidth
  const Bandwidth *bw = getCurrentBandwidth();
  if(bw) lv_label_set_text(lblBwVal, bw->desc);

  // Update AGC
  if(disableAgc)
  {
    char buf[8];
    snprintf(buf, sizeof(buf), "ATT%d", agcNdx);
    lv_label_set_text(lblAgcVal, buf);
  }
  else
  {
    lv_label_set_text(lblAgcVal, "AUTO");
  }

  // Update volume
  char volBuf[8];
  snprintf(volBuf, sizeof(volBuf), "%d", volume);
  lv_label_set_text(lblVolVal, volBuf);
}

void lvglTick()
{
  if(!_lvglActive) return;
  lv_timer_handler();
}

bool lvglActive()
{
  return _lvglActive;
}

void lvglSetActive(bool active)
{
  if(active && !_lvglActive)
  {
    _lvglActive = true;
    if(scrMain) lv_screen_load(scrMain);
    lv_obj_invalidate(scrMain);
    prevFrequency = 0xFFFF;
  }
  else if(!active)
  {
    _lvglActive = false;
  }
}
