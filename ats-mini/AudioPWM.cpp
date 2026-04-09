#include "AudioPWM.h"
#include <Arduino.h>

#define AUDIO_PWM_PIN   11
#define AUDIO_PWM_FREQ  40000    // 40kHz carrier (above audio range, easy to filter)
#define AUDIO_PWM_BITS  8        // 8-bit resolution (0-255)
#define SAMPLE_RATE     16000    // 16kHz audio sample rate
#define YIELD_EVERY     800      // Yield every 800 samples (~50ms) to feed WDT

// Sine lookup table (256 entries, 0-255)
static uint8_t sineTable[256];

static volatile bool running = false;

// Test tone sequence
struct ToneStep
{
  uint32_t freqHz;
  uint32_t durationMs;
  bool     square;
};

static const ToneStep testSequence[] = {
  { 1000, 2000, false },   // 1kHz sine, 2s
  { 4000, 3000, false },   // 4kHz sine, 3s
  {  440, 2000, false },   // 440Hz sine, 2s
  { 1000, 1000, true  },   // 1kHz square, 1s
};

#define TONE_COUNT  (sizeof(testSequence) / sizeof(testSequence[0]))

// FreeRTOS task that generates audio samples
static void audioTask(void *param)
{
  // Let system fully settle
  vTaskDelay(pdMS_TO_TICKS(2000));

  Serial.println("AudioPWM: Task started, generating tones...");

  const uint32_t samplePeriodUs = 1000000UL / SAMPLE_RATE;
  uint32_t yieldCounter = 0;

  for(int rep = 0; rep < 20; rep++)
  {
  Serial.printf("AudioPWM: Repeat %d/20\n", rep + 1);
  for(int t = 0; t < (int)TONE_COUNT; t++)
  {
    Serial.printf("AudioPWM: Tone %dHz %s, %dms\n",
                  testSequence[t].freqHz,
                  testSequence[t].square ? "square" : "sine",
                  testSequence[t].durationMs);
    uint32_t phaseInc = (uint32_t)((uint64_t)testSequence[t].freqHz * 65536ULL / SAMPLE_RATE);
    uint32_t totalSamples = (uint32_t)((uint64_t)testSequence[t].durationMs * SAMPLE_RATE / 1000);
    bool square = testSequence[t].square;
    uint32_t phase = 0;

    for(uint32_t i = 0; i < totalSamples; i++)
    {
      uint8_t sample;
      if(square)
        sample = (phase & 0x8000) ? 255 : 0;
      else
        sample = sineTable[(phase >> 8) & 0xFF];

      ledcWrite(AUDIO_PWM_PIN, sample);
      phase += phaseInc;

      delayMicroseconds(samplePeriodUs);

      // Periodically yield to let idle task + WDT run
      if(++yieldCounter >= YIELD_EVERY)
      {
        yieldCounter = 0;
        taskYIELD();
      }
    }
  }

  } // end repeat loop

  // Silence and done
  ledcWrite(AUDIO_PWM_PIN, 128);
  running = false;
  Serial.println("AudioPWM: Test sequence complete");
  vTaskDelete(NULL);
}

void audioPwmInit()
{
  // Build sine table
  for(int i = 0; i < 256; i++)
  {
    sineTable[i] = (uint8_t)(128.0 + 127.0 * sin(2.0 * M_PI * i / 256.0));
  }

  // Verify GPIO 11 can toggle (basic output test)
  pinMode(AUDIO_PWM_PIN, OUTPUT);
  digitalWrite(AUDIO_PWM_PIN, HIGH);
  delay(1);
  digitalWrite(AUDIO_PWM_PIN, LOW);

  // Configure PWM on GPIO 11
  bool ok = ledcAttach(AUDIO_PWM_PIN, AUDIO_PWM_FREQ, AUDIO_PWM_BITS);
  Serial.printf("AudioPWM: ledcAttach(pin=%d, freq=%d, bits=%d) = %s\n",
                AUDIO_PWM_PIN, AUDIO_PWM_FREQ, AUDIO_PWM_BITS, ok ? "OK" : "FAIL");

  if(ok)
  {
    ledcWrite(AUDIO_PWM_PIN, 128);  // Start at midpoint (silence)
    Serial.println("AudioPWM: Initialized (GPIO 11)");
  }
  else
  {
    Serial.println("AudioPWM: ERROR - ledcAttach failed! PWM will not work.");
  }
}

void audioPwmTestPlay()
{
  if(running) return;
  running = true;

  Serial.println("AudioPWM: Playing test sequence...");

  // Launch on core 0 (core 1 runs Arduino loop)
  xTaskCreatePinnedToCore(audioTask, "audioPWM", 4096, NULL, 5, NULL, 0);
}

bool audioPwmTestRunning()
{
  return running;
}
