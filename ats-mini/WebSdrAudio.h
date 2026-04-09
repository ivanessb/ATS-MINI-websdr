#ifndef WEBSDR_AUDIO_H
#define WEBSDR_AUDIO_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// ---------------------------------------------------------------------------
// WebSDR Audio Decode Pipeline — PA3FWM Compressed Codec
//
// Decodes the PA3FWM WebSDR binary protocol: mu-law blocks (0x80),
// compressed predictive audio (0x00-0x7F, 0x90-0xDF), parameter tags
// (0x81-0x85), silence (0x84), and S-meter (0xF0-0xFF).
// ---------------------------------------------------------------------------

// Internal decode buffer (decoded PCM ready for PwmAudio)
#define WEBSDR_AUDIO_BUF_SIZE  16384
#define WEBSDR_AUDIO_BUF_MASK  (WEBSDR_AUDIO_BUF_SIZE - 1)

#ifdef __cplusplus
extern "C" {
#endif

// Initialize the audio decode pipeline. Clears buffers and counters.
bool webSdrAudioInit(void);

// Reset decode state (e.g., on reconnect). Clears buffers.
void webSdrAudioReset(void);

// Feed raw WebSDR audio payload bytes. Returns number of bytes consumed.
// This function parses frame type bytes and decodes audio into PCM.
size_t webSdrAudioFeed(const uint8_t *data, size_t len);

// Read decoded unsigned 9-bit PCM samples from the internal buffer.
// Returns number of samples actually read.
size_t webSdrAudioReadPcm(uint16_t *out, size_t maxSamples);

// Returns number of decoded PCM samples available for reading.
size_t webSdrAudioAvailable(void);

// Flush the decoded PCM buffer (e.g., on tune change).
void webSdrAudioFlush(void);

// Diagnostic counters
uint32_t webSdrAudioGetUnknownFrameCount(void);
uint32_t webSdrAudioGetDecodeErrorCount(void);
uint32_t webSdrAudioGetBytesReceived(void);
uint32_t webSdrAudioGetPcmBytesProduced(void);
void webSdrAudioResetCounters(void);

// Protocol state getters
uint32_t webSdrAudioGetSampleRate(void);
uint16_t webSdrAudioGetSmeter(void);

#ifdef __cplusplus
}
#endif

#endif // WEBSDR_AUDIO_H
