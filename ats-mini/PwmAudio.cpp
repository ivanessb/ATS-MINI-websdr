#include "PwmAudio.h"
#include <Arduino.h>
#include <esp_timer.h>
#include <math.h>

// ---------------------------------------------------------------------------
// Ring buffer — lock-free single-producer / single-consumer.
// Only head/tail indices are volatile; data array does not need it
// since the ISR only reads via ringTail and the producer only writes
// via ringHead, with the index update acting as a release fence.
// ---------------------------------------------------------------------------

static uint16_t          ringBuf[PWM_AUDIO_BUF_SIZE];
static volatile uint32_t ringHead = 0;  // write position (producer)
static volatile uint32_t ringTail = 0;  // read  position (consumer / ISR)

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

static int          pwmPin        = -1;
static uint32_t     pwmSampleRate = 0;
static bool         pwmRunning    = false;
static bool         pwmMuted      = false;
static volatile uint8_t pwmVolume  = 255;  // full scale

static volatile uint32_t underrunCount = 0;
static volatile uint32_t overflowCount = 0;

static esp_timer_handle_t pwmTimer = NULL;

// Sine table for test tone generation (same as existing AudioPWM)
static uint16_t sineTable[256];
static bool sineTableBuilt = false;

// ---------------------------------------------------------------------------
// Helper: buffered byte count (safe to call from ISR or main)
// ---------------------------------------------------------------------------

static inline uint32_t bufferedBytes(void)
{
  return (ringHead - ringTail) & PWM_AUDIO_BUF_MASK;
}

static inline uint32_t freeBytes(void)
{
  // Reserve 1 byte to distinguish full from empty
  return PWM_AUDIO_BUF_SIZE - 1 - bufferedBytes();
}

// ---------------------------------------------------------------------------
// Timer callback — runs at sampleRate Hz, fetches one sample, writes PWM
// ---------------------------------------------------------------------------

static uint16_t lastSample = PWM_AUDIO_SILENCE;  // Track last output for smooth fade
static uint8_t  fadeCounter = 0;                  // Fade-to-silence on underrun

static void IRAM_ATTR pwmTimerISR(void *arg)
{
  uint16_t sample;

  if (ringHead != ringTail)
  {
    sample = ringBuf[ringTail];
    ringTail = (ringTail + 1) & PWM_AUDIO_BUF_MASK;
    lastSample = sample;
    fadeCounter = 0;
  }
  else
  {
    // Buffer underrun — fade to silence to avoid DC pop
    underrunCount++;
    if (lastSample != PWM_AUDIO_SILENCE && fadeCounter < 64)
    {
      // Exponential-ish fade: shift toward silence over ~64 samples (~8ms)
      int32_t s = (int32_t)lastSample;
      s = s + (((int32_t)PWM_AUDIO_SILENCE - s) * (int32_t)(fadeCounter + 1)) / 64;
      sample = (uint16_t)s;
      fadeCounter++;
      if (fadeCounter >= 64) lastSample = PWM_AUDIO_SILENCE;
    }
    else
    {
      sample = PWM_AUDIO_SILENCE;
    }
  }

  // Apply volume scaling
  if (pwmVolume < 255)
  {
    int32_t centered = (int32_t)sample - 512;
    centered = (centered * (int32_t)pwmVolume) >> 8;
    sample = (uint16_t)(centered + 512);
  }

  // Apply mute
  if (pwmMuted) sample = PWM_AUDIO_SILENCE;

  ledcWrite(pwmPin, sample);
}

// ---------------------------------------------------------------------------
// Build sine table (only once)
// ---------------------------------------------------------------------------

static void buildSineTable(void)
{
  if (sineTableBuilt) return;
  for (int i = 0; i < 256; i++)
  {
    sineTable[i] = (uint16_t)(512.0 + 511.0 * sin(2.0 * M_PI * i / 256.0));
  }
  sineTableBuilt = true;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool pwmAudioInit(uint32_t sampleRate, int pin)
{
  if (pwmRunning) pwmAudioStop();
  if (pwmTimer)   pwmAudioDeinit();

  pwmPin        = pin;
  pwmSampleRate = sampleRate;
  pwmRunning    = false;
  pwmMuted      = false;
  pwmVolume     = 255;
  ringHead      = 0;
  ringTail      = 0;
  underrunCount = 0;
  overflowCount = 0;

  buildSineTable();

  // Configure LEDC PWM on the pin
  pinMode(pwmPin, OUTPUT);
  bool ok = ledcAttach(pwmPin, PWM_AUDIO_CARRIER_FREQ, PWM_AUDIO_BITS);
  if (!ok)
  {
    Serial.printf("PwmAudio: ledcAttach(pin=%d) FAILED\n", pwmPin);
    return false;
  }

  // Start at silence
  ledcWrite(pwmPin, PWM_AUDIO_SILENCE);

  // Create high-resolution timer
  esp_timer_create_args_t timerArgs = {};
  timerArgs.callback = pwmTimerISR;
  timerArgs.name     = "pwmAudio";

  esp_err_t err = esp_timer_create(&timerArgs, &pwmTimer);
  if (err != ESP_OK)
  {
    Serial.printf("PwmAudio: timer create failed (%d)\n", err);
    return false;
  }

  Serial.printf("PwmAudio: init pin=%d rate=%lu OK\n", pwmPin, (unsigned long)sampleRate);
  return true;
}

void pwmAudioDeinit(void)
{
  pwmAudioStop();

  if (pwmTimer)
  {
    esp_timer_delete(pwmTimer);
    pwmTimer = NULL;
  }

  if (pwmPin >= 0)
  {
    ledcWrite(pwmPin, PWM_AUDIO_SILENCE);
    ledcDetach(pwmPin);
    pwmPin = -1;
  }
}

bool pwmAudioStart(void)
{
  if (pwmRunning) return true;
  if (!pwmTimer || pwmPin < 0) return false;

  uint64_t periodUs = 1000000ULL / pwmSampleRate;
  esp_err_t err = esp_timer_start_periodic(pwmTimer, periodUs);
  if (err != ESP_OK)
  {
    Serial.printf("PwmAudio: timer start failed (%d)\n", err);
    return false;
  }

  pwmRunning = true;
  Serial.println("PwmAudio: playback started");
  return true;
}

void pwmAudioStop(void)
{
  if (!pwmRunning) return;

  if (pwmTimer) esp_timer_stop(pwmTimer);

  pwmRunning = false;

  if (pwmPin >= 0) ledcWrite(pwmPin, PWM_AUDIO_SILENCE);

  Serial.println("PwmAudio: playback stopped");
}

bool pwmAudioIsRunning(void)
{
  return pwmRunning;
}

bool pwmAudioWriteSamples(const uint16_t *data, size_t len)
{
  size_t avail = freeBytes();
  if (len > avail)
  {
    // Overflow — write what we can, count the rest
    overflowCount++;
    len = avail;
  }

  for (size_t i = 0; i < len; i++)
  {
    ringBuf[ringHead] = data[i];
    ringHead = (ringHead + 1) & PWM_AUDIO_BUF_MASK;
  }

  return true;
}

size_t pwmAudioGetFreeSpace(void)
{
  return freeBytes();
}

size_t pwmAudioGetBufferedBytes(void)
{
  return bufferedBytes();
}

void pwmAudioSetVolume(uint8_t volume)
{
  pwmVolume = volume;
}

void pwmAudioFlush(void)
{
  ringHead = 0;
  ringTail = 0;
  lastSample = PWM_AUDIO_SILENCE;
  fadeCounter = 0;
}

void pwmAudioMute(bool enable)
{
  pwmMuted = enable;
}

uint32_t pwmAudioGetUnderrunCount(void)
{
  return underrunCount;
}

uint32_t pwmAudioGetOverflowCount(void)
{
  return overflowCount;
}

void pwmAudioResetCounters(void)
{
  underrunCount = 0;
  overflowCount = 0;
}

void pwmAudioTestTone(uint32_t freqHz, uint32_t durationMs)
{
  if (!pwmTimer || pwmPin < 0) return;

  buildSineTable();

  // Generate sine wave PCM data and push into buffer
  uint32_t totalSamples = (uint32_t)((uint64_t)durationMs * pwmSampleRate / 1000);
  uint32_t phaseInc = (uint32_t)((uint64_t)freqHz * 65536ULL / pwmSampleRate);
  uint32_t phase = 0;

  // Write in chunks to avoid blocking too long
  uint16_t chunk[128];
  uint32_t written = 0;

  while (written < totalSamples)
  {
    size_t avail = freeBytes();
    if (avail == 0) { delay(1); continue; }

    size_t count = totalSamples - written;
    if (count > (sizeof(chunk) / sizeof(chunk[0]))) count = (sizeof(chunk) / sizeof(chunk[0]));
    if (count > avail)         count = avail;

    for (size_t i = 0; i < count; i++)
    {
      chunk[i] = sineTable[(phase >> 8) & 0xFF];
      phase += phaseInc;
    }

    pwmAudioWriteSamples(chunk, count);
    written += count;
  }
}
