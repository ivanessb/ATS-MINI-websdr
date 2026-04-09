#include "WebSdrAudio.h"
#include <Arduino.h>
#include <string.h>

// ---------------------------------------------------------------------------
// WebSDR Audio Decode Pipeline — PA3FWM Compressed Codec
//
// Implements the PA3FWM WebSDR binary protocol. Each WebSocket binary
// message is a stream of tagged blocks:
//   0x80:       Mu-law block (128 bytes of mu-law encoded samples)
//   0x81 HH LL: Sample rate (16-bit BE)
//   0x82 HH LL: Quantization parameter Ot
//   0x83 XX:    Mode/filter info byte jt
//   0x84:       Silence block (128 zero samples)
//   0x85 + 6B:  True frequency
//   0x90-0xDF:  Compressed type A (sets Ut, decoder reads from tag byte)
//   0x00-0x7F:  Compressed type B (Ut unchanged)
//   0xF0-0xFF:  S-meter (low nibble * 256 + next byte)
//
// Compressed blocks use a 20-tap adaptive predictive codec with entropy
// coding, producing 128 Int16 samples per block. Ported from
// websdr-sound.js.
// ---------------------------------------------------------------------------

// Mu-law decode table (256 entries, from websdr-sound.js variable 'x')
static const int16_t ULAW[256] = {
  -5504,-5248,-6016,-5760,-4480,-4224,-4992,-4736,
  -7552,-7296,-8064,-7808,-6528,-6272,-7040,-6784,
  -2752,-2624,-3008,-2880,-2240,-2112,-2496,-2368,
  -3776,-3648,-4032,-3904,-3264,-3136,-3520,-3392,
  -22016,-20992,-24064,-23040,-17920,-16896,-19968,-18944,
  -30208,-29184,-32256,-31232,-26112,-25088,-28160,-27136,
  -11008,-10496,-12032,-11520,-8960,-8448,-9984,-9472,
  -15104,-14592,-16128,-15616,-13056,-12544,-14080,-13568,
  -344,-328,-376,-360,-280,-264,-312,-296,
  -472,-456,-504,-488,-408,-392,-440,-424,
  -88,-72,-120,-104,-24,-8,-56,-40,
  -216,-200,-248,-232,-152,-136,-184,-168,
  -1376,-1312,-1504,-1440,-1120,-1056,-1248,-1184,
  -1888,-1824,-2016,-1952,-1632,-1568,-1760,-1696,
  -688,-656,-752,-720,-560,-528,-624,-592,
  -944,-912,-1008,-976,-816,-784,-880,-848,
  5504,5248,6016,5760,4480,4224,4992,4736,
  7552,7296,8064,7808,6528,6272,7040,6784,
  2752,2624,3008,2880,2240,2112,2496,2368,
  3776,3648,4032,3904,3264,3136,3520,3392,
  22016,20992,24064,23040,17920,16896,19968,18944,
  30208,29184,32256,31232,26112,25088,28160,27136,
  11008,10496,12032,11520,8960,8448,9984,9472,
  15104,14592,16128,15616,13056,12544,14080,13568,
  344,328,376,360,280,264,312,296,
  472,456,504,488,408,392,440,424,
  88,72,120,104,24,8,56,40,
  216,200,248,232,152,136,184,168,
  1376,1312,1504,1440,1120,1056,1248,1184,
  1888,1824,2016,1952,1632,1568,1760,1696,
  688,656,752,720,560,528,624,592,
  944,912,1008,976,816,784,880,848
};

// Threshold table for z calculation
static const int S_TABLE[8] = { 999, 999, 8, 4, 2, 1, 99, 99 };

// ---------------------------------------------------------------------------
// Ring buffer for decoded PCM (unsigned 9-bit for PwmAudio)
// ---------------------------------------------------------------------------

static uint16_t pcmBuf[WEBSDR_AUDIO_BUF_SIZE];
static uint32_t pcmHead = 0;
static uint32_t pcmTail = 0;
static uint32_t pcmDropCount = 0;

static inline uint32_t pcmBuffered(void) { return (pcmHead - pcmTail) & WEBSDR_AUDIO_BUF_MASK; }
static inline uint32_t pcmFree(void)     { return WEBSDR_AUDIO_BUF_SIZE - 1 - pcmBuffered(); }

static void pcmWrite(uint16_t sample)
{
  if (pcmFree() == 0) { pcmDropCount++; return; }
  pcmBuf[pcmHead] = sample;
  pcmHead = (pcmHead + 1) & WEBSDR_AUDIO_BUF_MASK;
}

// ---------------------------------------------------------------------------
// Protocol state
// ---------------------------------------------------------------------------

static int32_t  Ot = 40;          // quantization parameter (0x82 tag)
static int      Ut = 0;           // shift parameter (set by type A blocks)
static int      jt = 0;           // mode info byte (0x83 tag)
static uint16_t smeter_val = 0;   // S-meter reading
static uint32_t currentSampleRate = 8000;

// 20-tap adaptive predictor state (matching JS: qt, Kt, Qt)
static int32_t qt[20];
static int32_t Kt[20];
static int32_t Qt = 0;

// ---------------------------------------------------------------------------
// Diagnostic counters
// ---------------------------------------------------------------------------

static uint32_t unknownFrameCount = 0;
static uint32_t decodeErrorCount  = 0;
static uint32_t bytesReceived     = 0;
static uint32_t pcmBytesProduced  = 0;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static inline uint16_t s16ToU10(int32_t s)
{
  if (s > 32767)  s = 32767;
  if (s < -32768) s = -32768;
  return (uint16_t)((s >> 6) + 512);
}

static void emitSample(int32_t sample)
{
  pcmWrite(s16ToU10(sample));
  pcmBytesProduced++;
}

static void resetPred(void)
{
  memset(qt, 0, sizeof(qt));
  memset(Kt, 0, sizeof(Kt));
  Qt = 0;
}

// ---------------------------------------------------------------------------
// Compressed audio decoder — ported from websdr-sound.js
//
// 20-tap adaptive predictor with entropy-coded residuals.
// Decodes exactly 128 Int16 samples per block.
//
// Parameters:
//   t, tlen  — input byte array and its length
//   n_start  — starting byte offset (points to the tag byte)
//   u_start  — starting bit offset within that byte (4 for type A, 1 for B)
//
// Returns the final byte offset (caller adds 1 to advance past it).
// ---------------------------------------------------------------------------

static size_t decodeCompressed(const uint8_t *t, size_t tlen, size_t n_start, int u_start)
{
  size_t n = n_start;
  int u = u_start;
  int f = (jt & 16) ? 12 : 14;

  for (int sc = 0; sc < 128; sc++)
  {
    // Read 4 bytes from current position (0 if beyond buffer, matching JS)
    uint8_t b0 = (n     < tlen) ? t[n]     : 0;
    uint8_t b1 = (n + 1 < tlen) ? t[n + 1] : 0;
    uint8_t b2 = (n + 2 < tlen) ? t[n + 2] : 0;
    uint8_t b3 = (n + 3 < tlen) ? t[n + 3] : 0;
    uint32_t w = ((uint32_t)b0 << 24) | ((uint32_t)b1 << 16)
               | ((uint32_t)b2 << 8)  | b3;

    int d = 0;
    int underscore = 15 - Ut;
    int T = Ot;

    w <<= u;

    if (w != 0)
    {
      while ((w & 0x80000000U) == 0 && d < underscore)
      {
        w <<= 1;
        d++;
      }
      if (d < underscore)
      {
        underscore = d;
        d++;
        w <<= 1;
      }
      else
      {
        underscore = (w >> 24) & 0xFF;
        d += 8;
        w <<= 8;
      }
    }
    else
    {
      underscore = 0;
      d += 8;
    }

    // Calculate z (extra mantissa bits)
    int z = 0;
    if (Ut >= 1)
    {
      if (underscore >= S_TABLE[Ut])     z++;
      if (underscore >= S_TABLE[Ut - 1]) z++;
      if (z > Ut - 1) z = Ut - 1;
    }

    // Extract mantissa: JS (-1<<z)&0xFFFF
    uint16_t mask = (uint16_t)(~((1U << z) - 1));
    int32_t S_val = (int32_t)((((w >> 16) & 0xFFFF) >> (17 - Ut)) & mask);
    S_val += underscore << (Ut - 1);

    // Check sign bit
    int sign_bit = 32 - Ut + z;
    if (sign_bit < 32 && (w & (1U << sign_bit)))
    {
      S_val = ~(S_val | ((1 << z) - 1));
    }

    // Advance bit position
    u += d + Ut - z;
    while (u >= 8)
    {
      n++;
      u -= 8;
    }

    // Compute prediction: sum(qt[i]*Kt[i]), truncated to 32-bit
    int64_t pred_acc = 0;
    for (int i = 0; i < 20; i++)
      pred_acc += (int64_t)qt[i] * Kt[i];
    int32_t ps = (int32_t)pred_acc;

    int32_t pred_out;
    if (ps >= 0)
      pred_out = ps >> 12;
    else
      pred_out = (ps + 4095) >> 12;

    // Scale residual: T_val = S_val * Ot + Ot/2
    int32_t T_val = (int32_t)(S_val * T + (T >> 1));
    int32_t S_scaled = T_val >> 4;

    // Update prediction coefficients (20-tap adaptive filter)
    for (int i = 19; i > 0; i--)
    {
      int32_t decay = -(qt[i] >> 7);
      int32_t adapt = (int32_t)((int64_t)Kt[i] * S_scaled) >> f;
      qt[i] += decay + adapt;
      Kt[i] = Kt[i - 1];
    }
    {
      int32_t decay = -(qt[0] >> 7);
      int32_t adapt = (int32_t)((int64_t)Kt[0] * S_scaled) >> f;
      qt[0] += decay + adapt;
    }

    Kt[0] = pred_out + T_val;

    // Output sample with DC offset tracking
    int32_t sample = Kt[0] + (Qt >> 4);
    if (jt & 16)
      Qt = 0;
    else
      Qt += (int32_t)((uint32_t)Kt[0] << 4) >> 3;

    emitSample(sample);
  }

  if (u == 0) n--;
  return n;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool webSdrAudioInit(void)
{
  pcmHead = 0;
  pcmTail = 0;
  Ot = 40;
  Ut = 0;
  jt = 0;
  smeter_val = 0;
  currentSampleRate = 8000;
  resetPred();
  unknownFrameCount = 0;
  decodeErrorCount  = 0;
  bytesReceived     = 0;
  pcmBytesProduced  = 0;
  return true;
}

void webSdrAudioReset(void)
{
  pcmHead = 0;
  pcmTail = 0;
  resetPred();
}

size_t webSdrAudioFeed(const uint8_t *data, size_t len)
{
  if (!data || len < 1) return 0;

  bytesReceived += len;

  size_t n = 0;
  while (n < len)
  {
    uint8_t b = data[n];

    if ((b & 0xF0) == 0xF0)
    {
      // S-meter: low_nibble * 256 + next_byte
      if (n + 1 < len)
      {
        smeter_val = (uint16_t)((b & 0x0F) * 256 + data[n + 1]);
        n += 2;
      }
      else
        break;
    }
    else if (b == 0x80)
    {
      // Mu-law block: 128 bytes of mu-law encoded samples
      if (n + 128 < len)
      {
        for (int i = 0; i < 128; i++)
          emitSample(ULAW[data[n + 1 + i]]);
        n += 129;
        resetPred();
      }
      else
        break;
    }
    else if (b >= 0x90 && b <= 0xDF)
    {
      // Compressed type A: sets Ut, reads from tag byte with u=4
      Ut = 14 - (b >> 4);
      n = decodeCompressed(data, len, n, 4);
      n++;
    }
    else if ((b & 0x80) == 0)
    {
      // Compressed type B: Ut unchanged, reads from this byte with u=1
      n = decodeCompressed(data, len, n, 1);
      n++;
    }
    else if (b == 0x81)
    {
      // Sample rate: 16-bit big-endian
      if (n + 2 < len)
      {
        uint32_t newRate = (uint32_t)data[n + 1] * 256 + data[n + 2];
        if (newRate > 0)
          currentSampleRate = newRate;
        n += 3;
      }
      else
        break;
    }
    else if (b == 0x82)
    {
      // Quantization parameter Ot: 16-bit big-endian
      if (n + 2 < len)
      {
        Ot = (int32_t)data[n + 1] * 256 + data[n + 2];
        n += 3;
      }
      else
        break;
    }
    else if (b == 0x83)
    {
      // Mode info byte jt
      if (n + 1 < len)
      {
        jt = data[n + 1];
        n += 2;
      }
      else
        break;
    }
    else if (b == 0x84)
    {
      // Silence block: 128 zero samples
      for (int i = 0; i < 128; i++)
        emitSample(0);
      resetPred();
      n++;
    }
    else if (b == 0x85)
    {
      // True frequency: 6 bytes of data
      if (n + 6 < len)
        n += 7;
      else
        break;
    }
    else
    {
      // Unknown tag
      unknownFrameCount++;
      n++;
    }
  }

  return len;
}

size_t webSdrAudioReadPcm(uint16_t *out, size_t maxSamples)
{
  size_t avail = pcmBuffered();
  if (maxSamples > avail) maxSamples = avail;

  for (size_t i = 0; i < maxSamples; i++)
  {
    out[i] = pcmBuf[pcmTail];
    pcmTail = (pcmTail + 1) & WEBSDR_AUDIO_BUF_MASK;
  }

  return maxSamples;
}

size_t webSdrAudioAvailable(void)
{
  return pcmBuffered();
}

void webSdrAudioFlush(void)
{
  pcmHead = 0;
  pcmTail = 0;
}

uint32_t webSdrAudioGetUnknownFrameCount(void) { return unknownFrameCount; }
uint32_t webSdrAudioGetDecodeErrorCount(void)  { return decodeErrorCount; }
uint32_t webSdrAudioGetBytesReceived(void)     { return bytesReceived; }
uint32_t webSdrAudioGetPcmBytesProduced(void)  { return pcmBytesProduced; }
uint32_t webSdrAudioGetSampleRate(void)        { return currentSampleRate; }
uint16_t webSdrAudioGetSmeter(void)            { return smeter_val; }

void webSdrAudioResetCounters(void)
{
  unknownFrameCount = 0;
  decodeErrorCount  = 0;
  bytesReceived     = 0;
  pcmBytesProduced  = 0;
}
