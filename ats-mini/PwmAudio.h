#ifndef PWM_AUDIO_H
#define PWM_AUDIO_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// Default configuration matching the existing AudioPWM setup
#define PWM_AUDIO_DEFAULT_PIN         11
#define PWM_AUDIO_DEFAULT_SAMPLE_RATE 8000
#define PWM_AUDIO_CARRIER_FREQ        39000
#define PWM_AUDIO_BITS                10
#define PWM_AUDIO_SILENCE             512

// Ring buffer size — must be power of 2
// 8192 samples = ~1024ms at 8kHz, enough to absorb jitter
#define PWM_AUDIO_BUF_SIZE            8192
#define PWM_AUDIO_BUF_MASK            (PWM_AUDIO_BUF_SIZE - 1)

#ifdef __cplusplus
extern "C" {
#endif

// Initialize PWM audio on the given pin at the given sample rate.
// Returns true on success.
bool pwmAudioInit(uint32_t sampleRate, int pin);

// Deinitialize PWM audio, stop timer, release pin.
void pwmAudioDeinit(void);

// Start playback (timer begins consuming buffer).
bool pwmAudioStart(void);

// Stop playback (timer stops, output set to silence).
void pwmAudioStop(void);

// Returns true if playback timer is running.
bool pwmAudioIsRunning(void);

// Write unsigned 9-bit PCM samples into the ring buffer.
// Returns true if all samples were written, false if overflow occurred.
bool pwmAudioWriteSamples(const uint16_t *data, size_t len);

// Returns number of free bytes in the ring buffer.
size_t pwmAudioGetFreeSpace(void);

// Returns number of buffered bytes available for playback.
size_t pwmAudioGetBufferedBytes(void);

// Set playback volume (0-255). 255 = full scale.
void pwmAudioSetVolume(uint8_t volume);

// Flush the playback ring buffer (e.g., on tune change).
void pwmAudioFlush(void);

// Mute/unmute output (muted = output silence, keep consuming buffer).
void pwmAudioMute(bool enable);

// Diagnostic counters
uint32_t pwmAudioGetUnderrunCount(void);
uint32_t pwmAudioGetOverflowCount(void);
void pwmAudioResetCounters(void);

// Test: play a sine wave test tone at the given frequency for durationMs.
// Non-blocking — fills the buffer with sine data.
void pwmAudioTestTone(uint32_t freqHz, uint32_t durationMs);

#ifdef __cplusplus
}
#endif

#endif // PWM_AUDIO_H
