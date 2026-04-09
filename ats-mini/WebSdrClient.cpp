#include "WebSdrClient.h"
#include "WebSdrAudio.h"
#include "PwmAudio.h"
#include "Common.h"
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <esp_wifi.h>
#include <lwip/sockets.h>

// ---------------------------------------------------------------------------
// Protocol constants
//
// ASSUMPTION: WebSDR uses a custom WebSocket-like protocol.
// The stream endpoint is "/~~stream" and control parameters are sent
// as text frames. Binary frames carry audio data.
//
// These constants are isolated here so they can be adjusted if the
// server behavior differs from expectations.
// ---------------------------------------------------------------------------

#define WEBSDR_STREAM_PATH    "/~~stream"
#define WEBSDR_WS_KEY         "dGhlIHNhbXBsZSBub25jZQ=="  // Standard WebSocket key for handshake
#define WEBSDR_WS_GUID        "258EAFA5-E914-47DA-95CA-5AB5DC76E5B3"

#define WEBSDR_RECONNECT_DELAY_MS  3000
#define WEBSDR_KEEPALIVE_MS       15000
#define WEBSDR_CONNECT_TIMEOUT_MS 10000
#define WEBSDR_RECV_BUF_SIZE      16384

// Audio pump: how many decoded PCM samples to push from WebSdrAudio → PwmAudio per task call
#define AUDIO_PUMP_CHUNK  256

// Pre-buffer: accumulate this many decoded PCM samples before starting playback
// 4096 samples = 512ms at 8kHz — enough cushion to absorb WiFi jitter
#define PREBUFFER_SAMPLES 4096

// Tune command rate limit (ms between sends)
#define TUNE_THROTTLE_MS  150

// ---------------------------------------------------------------------------
// Server table — initially one entry, structured for future additions
// ---------------------------------------------------------------------------

static const WebSdrServer servers[] = {
  {
    "Maasbree WebSDR",               // name
    "sdr.websdrmaasbree.nl",         // host
    8901,                            // port
    "/"                              // path
  },
};

#define SERVER_COUNT  (sizeof(servers) / sizeof(servers[0]))

// ---------------------------------------------------------------------------
// Band table — defaults for sdr.websdrmaasbree.nl
//
// Band ranges derived from bandinfo.js: centerfreq ± samplerate/2.
// ---------------------------------------------------------------------------

static const WebSdrBand bandTable[] = {
  { "160m",  0,   1799,   1991 },   // center=1895, sr=192
  { "80m",   1,   3468,   3852 },   // center=3660, sr=384
  { "60m",   2,   5277,   5469 },   // center=5373, sr=192
  { "40m",   3,   6908,   7292 },   // center=7100, sr=384
  { "30m",   4,  10054,  10246 },   // center=10150, sr=192
  { "20m",   5,  13983,  14367 },   // center=14175, sr=384
  { "17m",   6,  18022,  18214 },   // center=18118, sr=192
  { "15m",   7,  20841,  21609 },   // center=21225, sr=768
};

#define BAND_TABLE_COUNT  (sizeof(bandTable) / sizeof(bandTable[0]))

// ---------------------------------------------------------------------------
// Runtime state
// ---------------------------------------------------------------------------

static WebSdrState state;
static WiFiClient  tcpClient;
static uint8_t     recvBuf[WEBSDR_RECV_BUF_SIZE];

static uint32_t    reconnectTime  = 0;
static uint32_t    lastKeepalive  = 0;
static bool        wsUpgraded     = false;
static bool        audioStarted   = false;

// Tune throttle state
static uint32_t    lastTuneSent   = 0;
static bool        tunePending    = false;

// Receive buffer for WebSocket frame assembly
static uint8_t     frameBuf[WEBSDR_RECV_BUF_SIZE];
static size_t      frameLen = 0;

// ---------------------------------------------------------------------------
// Dual-core: network task on Core 0, command queue from Core 1
// ---------------------------------------------------------------------------

static TaskHandle_t networkTaskHandle = NULL;
static volatile bool networkTaskRunning = false;

// Thread-safe command queue (Core 1 enqueues, Core 0 sends)
static char         queuedCmd[128] = {0};
static volatile bool queuedCmdReady = false;
static portMUX_TYPE  cmdMux = portMUX_INITIALIZER_UNLOCKED;

// ---------------------------------------------------------------------------
// WebSocket frame parsing state
// ---------------------------------------------------------------------------

typedef enum {
  WS_PARSE_OPCODE,
  WS_PARSE_LEN,
  WS_PARSE_LEN16_1,
  WS_PARSE_LEN16_2,
  WS_PARSE_PAYLOAD
} WsParseState;

static WsParseState wsParseState  = WS_PARSE_OPCODE;
static uint8_t      wsOpcode      = 0;
static uint32_t     wsPayloadLen  = 0;
static uint32_t     wsPayloadRead = 0;
static bool         wsFin         = false;

// ---------------------------------------------------------------------------
// Forward declarations
// ---------------------------------------------------------------------------

static bool doConnect(void);
static bool doHandshake(void);
static void doReceive(void);
static void doReconnect(void);
static bool sendCommand(const char *text);
static void sendTuneCommand(void);
static void pumpAudio(void);

// ---------------------------------------------------------------------------
// WebSocket handshake — HTTP/1.1 Upgrade to /~~stream?v=11
// ---------------------------------------------------------------------------

static bool doHandshake(void)
{
  const WebSdrServer *srv = &servers[state.selectedServer];

  // Send WebSocket upgrade request with Origin header (required by many servers)
  char request[512];
  snprintf(request, sizeof(request),
    "GET /~~stream?v=11 HTTP/1.1\r\n"
    "Host: %s:%d\r\n"
    "Origin: http://%s:%d\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    "Sec-WebSocket-Key: %s\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n",
    srv->host, srv->port,
    srv->host, srv->port,
    WEBSDR_WS_KEY
  );

  Serial.printf("WebSDR: WS upgrade to %s:%d/~~stream?v=11\n",
                 srv->host, srv->port);
  Serial.printf("WebSDR: request len=%d\n", (int)strlen(request));

  size_t written = tcpClient.write((const uint8_t *)request, strlen(request));
  tcpClient.flush();
  Serial.printf("WebSDR: wrote %d bytes\n", (int)written);

  // Read HTTP response headers, look for "101"
  uint32_t start = millis();
  bool got101 = false;
  int lineCount = 0;

  while (millis() - start < WEBSDR_CONNECT_TIMEOUT_MS)
  {
    if (!tcpClient.connected())
    {
      Serial.printf("WebSDR: connection closed during handshake (after %lu ms, %d lines, avail=%d)\n",
                     millis() - start, lineCount, tcpClient.available());
      return false;
    }

    if (tcpClient.available())
    {
      String line = tcpClient.readStringUntil('\n');
      line.trim();
      lineCount++;

      Serial.printf("WebSDR: hdr[%d]: %s\n", lineCount, line.c_str());

      if (line.startsWith("HTTP/") && line.indexOf("101") >= 0)
        got101 = true;

      if (line.length() == 0)
      {
        // Empty line = end of headers
        if (got101)
        {
          Serial.println("WebSDR: WebSocket upgrade successful");
          wsUpgraded = true;
          wsParseState = WS_PARSE_OPCODE;
          return true;
        }
        else
        {
          Serial.println("WebSDR: server rejected WebSocket upgrade");
          return false;
        }
      }
    }
    else
    {
      delay(10);
    }
  }

  Serial.println("WebSDR: handshake timeout");
  return false;
}

// ---------------------------------------------------------------------------
// TCP connection
// ---------------------------------------------------------------------------

static bool doConnect(void)
{
  const WebSdrServer *srv = &servers[state.selectedServer];

  Serial.printf("WebSDR: connecting to %s:%d\n", srv->host, srv->port);

  tcpClient.setTimeout(WEBSDR_CONNECT_TIMEOUT_MS);

  if (!tcpClient.connect(srv->host, srv->port))
  {
    Serial.println("WebSDR: TCP connect failed");
    state.connectFailures++;
    return false;
  }

  // Disable Nagle algorithm — send data immediately
  tcpClient.setNoDelay(true);

  // Increase TCP receive buffer to absorb WiFi jitter
  int fd = tcpClient.fd();
  if (fd >= 0)
  {
    int rcvbuf = 16384;
    setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
    Serial.printf("WebSDR: TCP socket buf=%d, NoDelay=on\n", rcvbuf);
  }

  Serial.println("WebSDR: TCP connected");
  return true;
}

// ---------------------------------------------------------------------------
// Reconnect logic (state-driven)
// ---------------------------------------------------------------------------

static void doReconnect(void)
{
  tcpClient.stop();
  state.reconnectCount++;
  state.connState = WEBSDR_STATE_RECONNECT_WAIT;
  reconnectTime = millis();
  Serial.printf("WebSDR: reconnect scheduled (attempt %lu)\n",
                 (unsigned long)state.reconnectCount);
}

// ---------------------------------------------------------------------------
// WebSocket text frame send (masked, per RFC 6455)
// ---------------------------------------------------------------------------

static bool sendCommand(const char *text)
{
  if (!tcpClient.connected() || !wsUpgraded) return false;

  size_t len = strlen(text);
  uint8_t header[10];
  size_t headerLen = 0;

  header[0] = 0x81;  // FIN + text opcode

  if (len < 126)
  {
    header[1] = 0x80 | (uint8_t)len;
    headerLen = 2;
  }
  else
  {
    header[1] = 0x80 | 126;
    header[2] = (len >> 8) & 0xFF;
    header[3] = len & 0xFF;
    headerLen = 4;
  }

  // Generate random mask key
  uint32_t maskWord = esp_random();
  uint8_t mask[4];
  mask[0] = (maskWord >> 24) & 0xFF;
  mask[1] = (maskWord >> 16) & 0xFF;
  mask[2] = (maskWord >> 8) & 0xFF;
  mask[3] = maskWord & 0xFF;

  memcpy(header + headerLen, mask, 4);
  headerLen += 4;

  tcpClient.write(header, headerLen);

  // Send masked payload
  uint8_t masked[256];
  size_t sendLen = (len < sizeof(masked)) ? len : sizeof(masked);
  for (size_t i = 0; i < sendLen; i++)
    masked[i] = ((const uint8_t *)text)[i] ^ mask[i & 3];

  tcpClient.write(masked, sendLen);
  return true;
}

// ---------------------------------------------------------------------------
// Send tune command — correct PA3FWM WebSDR format
//
// Format: GET /~~param?f=FREQ&band=BAND&lo=LO&hi=HI&mode=MODE
// Mode encoding: CW/LSB/USB=0, AM=1, FM=4
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Build tune command string from current state
// ---------------------------------------------------------------------------

static void buildTuneCmd(char *buf, size_t bufLen)
{
  // Mode encoding: AM=1, LSB/USB/CW=0, FM=4
  static const int modeMap[] = { 1, 0, 0, 0, 4 };
  int mode = (state.currentMod < WEBSDR_MOD_COUNT) ? modeMap[state.currentMod] : 1;

  // Filter bandwidth based on modulation
  float lo = -4.0f, hi = 4.0f;
  switch (state.currentMod)
  {
    case WEBSDR_MOD_LSB: lo = -2.8f; hi = -0.3f; break;
    case WEBSDR_MOD_USB: lo = 0.3f;  hi = 2.8f;  break;
    case WEBSDR_MOD_CW:  lo = -0.5f; hi = 0.5f;  break;
    case WEBSDR_MOD_FM:  lo = -8.0f; hi = 8.0f;  break;
    default:             lo = -4.0f; hi = 4.0f;  break;
  }

  snprintf(buf, bufLen,
    "GET /~~param?f=%lu.%lu&band=%d&lo=%.1f&hi=%.1f&mode=%d",
    (unsigned long)(state.currentFreq / 10),
    (unsigned long)(state.currentFreq % 10),
    bandTable[state.currentBand].bandId,
    lo, hi, mode
  );
}

// ---------------------------------------------------------------------------
// Send tune command directly (only call from Core 0 / network task)
// ---------------------------------------------------------------------------

static void sendTuneCommand(void)
{
  if (state.connState != WEBSDR_STATE_STREAMING) return;

  char cmd[128];
  buildTuneCmd(cmd, sizeof(cmd));
  sendCommand(cmd);

  // Flush both audio buffers so the new frequency is heard immediately
  webSdrAudioFlush();
  pwmAudioFlush();
  audioStarted = false;  // Re-prebuffer for clean start
}

// ---------------------------------------------------------------------------
// Queue a tune command for the network task (thread-safe, call from Core 1)
// ---------------------------------------------------------------------------

static void queueTuneCommand(void)
{
  char cmd[128];
  buildTuneCmd(cmd, sizeof(cmd));

  taskENTER_CRITICAL(&cmdMux);
  strncpy(queuedCmd, cmd, sizeof(queuedCmd) - 1);
  queuedCmd[sizeof(queuedCmd) - 1] = '\0';
  queuedCmdReady = true;
  taskEXIT_CRITICAL(&cmdMux);
}

// ---------------------------------------------------------------------------
// WebSocket frame parser
//
// Parses incoming WebSocket frames byte-by-byte. This is a simple
// state machine that handles:
//   - Text frames (opcode 0x01) — control/status messages from server
//   - Binary frames (opcode 0x02) — audio data
//   - Ping frames (opcode 0x09) — responded to with pong
//   - Close frames (opcode 0x08) — triggers reconnect
//
// ASSUMPTION: Server frames are unmasked (server-to-client per RFC).
// Frames larger than WEBSDR_RECV_BUF_SIZE are truncated.
// ---------------------------------------------------------------------------

static void processFrame(uint8_t opcode, const uint8_t *data, size_t len)
{
  switch (opcode & 0x0F)
  {
    case 0x01:  // Text frame — control/status message
    {
      // Log for debugging protocol (non-audio path, safe to log)
      char textBuf[128];
      size_t copyLen = len < sizeof(textBuf) - 1 ? len : sizeof(textBuf) - 1;
      memcpy(textBuf, data, copyLen);
      textBuf[copyLen] = '\0';
      Serial.printf("WebSDR: text frame: %s\n", textBuf);
      break;
    }

    case 0x02:  // Binary frame — audio data
      webSdrAudioFeed(data, len);
      state.bytesReceived += len;
      break;

    case 0x08:  // Close frame
      Serial.println("WebSDR: server sent close frame");
      doReconnect();
      break;

    case 0x09:  // Ping — respond with masked pong
    {
      uint32_t maskWord = esp_random();
      uint8_t pong[6] = {
        0x8A, 0x80,  // FIN + PONG, masked, 0 length
        (uint8_t)(maskWord >> 24), (uint8_t)(maskWord >> 16),
        (uint8_t)(maskWord >> 8), (uint8_t)maskWord
      };
      tcpClient.write(pong, 6);
      break;
    }

    case 0x0A:  // Pong — ignore
      break;

    default:
      Serial.printf("WebSDR: unknown WS opcode 0x%02X len=%u\n", opcode, (unsigned)len);
      break;
  }
}

static void parseWebSocketData(const uint8_t *data, size_t len)
{
  for (size_t i = 0; i < len; i++)
  {
    uint8_t b = data[i];

    switch (wsParseState)
    {
      case WS_PARSE_OPCODE:
        wsFin = (b & 0x80) != 0;
        wsOpcode = b & 0x0F;
        wsParseState = WS_PARSE_LEN;
        break;

      case WS_PARSE_LEN:
      {
        uint8_t payloadLen7 = b & 0x7F;
        // ASSUMPTION: Server does not mask frames (server-to-client)
        if (payloadLen7 < 126)
        {
          wsPayloadLen = payloadLen7;
          wsPayloadRead = 0;
          frameLen = 0;
          wsParseState = (wsPayloadLen > 0) ? WS_PARSE_PAYLOAD : WS_PARSE_OPCODE;
          if (wsPayloadLen == 0) processFrame(wsOpcode, NULL, 0);
        }
        else if (payloadLen7 == 126)
        {
          wsPayloadLen = 0;
          wsParseState = WS_PARSE_LEN16_1;
        }
        else
        {
          // 64-bit length — not expected from WebSDR, treat as error
          Serial.println("WebSDR: 64-bit WS frame length, reconnecting");
          doReconnect();
          return;
        }
        break;
      }

      case WS_PARSE_LEN16_1:
        wsPayloadLen = (uint32_t)b << 8;
        wsParseState = WS_PARSE_LEN16_2;
        break;

      case WS_PARSE_LEN16_2:
        wsPayloadLen |= b;
        wsPayloadRead = 0;
        frameLen = 0;
        wsParseState = (wsPayloadLen > 0) ? WS_PARSE_PAYLOAD : WS_PARSE_OPCODE;
        if (wsPayloadLen == 0) processFrame(wsOpcode, NULL, 0);
        break;

      case WS_PARSE_PAYLOAD:
        if (frameLen < sizeof(frameBuf))
        {
          frameBuf[frameLen++] = b;
        }
        else if (frameLen == sizeof(frameBuf))
        {
          Serial.printf("WebSDR: WS frame truncated (payload=%lu, buf=%u)\n",
                         (unsigned long)wsPayloadLen, (unsigned)sizeof(frameBuf));
          frameLen++;  // only warn once per frame
        }
        wsPayloadRead++;
        if (wsPayloadRead >= wsPayloadLen)
        {
          // Skip truncated binary frames — they produce corrupted audio
          bool truncated = (frameLen > sizeof(frameBuf));
          size_t actualLen = truncated ? sizeof(frameBuf) : frameLen;
          if (truncated && (wsOpcode & 0x0F) == 0x02)
          {
            // Binary audio frame was truncated — discard to avoid garbled audio
          }
          else
          {
            processFrame(wsOpcode, frameBuf, actualLen);
          }
          wsParseState = WS_PARSE_OPCODE;
        }
        break;
    }
  }
}

// ---------------------------------------------------------------------------
// Receive data from TCP and parse WebSocket frames
// ---------------------------------------------------------------------------

static void doReceive(void)
{
  if (!tcpClient.connected()) return;

  // Route TCP data through WebSocket frame parser.
  // Interleave pumpAudio() after each chunk so the PWM buffer stays
  // topped-up during long bursts of TCP data (decode is CPU-intensive
  // and can starve the ISR if we process everything first).
  while (tcpClient.available())
  {
    int avail = tcpClient.available();
    if (avail > (int)sizeof(recvBuf)) avail = sizeof(recvBuf);

    int n = tcpClient.read(recvBuf, avail);
    if (n <= 0) break;

    parseWebSocketData(recvBuf, n);

    // Keep PWM buffer fed while decoding large bursts
    pumpAudio();
  }
}

// ---------------------------------------------------------------------------
// Audio pump: move decoded PCM from WebSdrAudio → PwmAudio ring buffer
// ---------------------------------------------------------------------------

static uint32_t lastAudioSampleRate = 8000;

static void pumpAudio(void)
{
  // Check for sample rate change from server
  uint32_t sr = webSdrAudioGetSampleRate();
  if (sr != lastAudioSampleRate && sr > 0)
  {
    Serial.printf("WebSDR: sample rate changed to %lu Hz\n", (unsigned long)sr);
    lastAudioSampleRate = sr;
    pwmAudioStop();
    pwmAudioDeinit();
    pwmAudioInit(sr, PWM_AUDIO_DEFAULT_PIN);
    audioStarted = false;  // Will re-start after pre-buffering
  }

  // Pre-buffer: wait until we have enough decoded samples before starting playback
  if (!audioStarted)
  {
    if (webSdrAudioAvailable() >= PREBUFFER_SAMPLES)
    {
      Serial.printf("WebSDR: pre-buffer filled (%u samples), starting playback\n",
                     (unsigned)webSdrAudioAvailable());
      pwmAudioStart();
      audioStarted = true;
    }
    else
    {
      return;  // Keep accumulating
    }
  }

  uint16_t chunk[AUDIO_PUMP_CHUNK];

  while (webSdrAudioAvailable() > 0 && pwmAudioGetFreeSpace() > 0)
  {
    size_t toRead = webSdrAudioAvailable();
    if (toRead > AUDIO_PUMP_CHUNK)       toRead = AUDIO_PUMP_CHUNK;
    if (toRead > pwmAudioGetFreeSpace())  toRead = pwmAudioGetFreeSpace();

    size_t got = webSdrAudioReadPcm(chunk, toRead);
    if (got == 0) break;

    pwmAudioWriteSamples(chunk, got);
  }
}

// ---------------------------------------------------------------------------
// Network task — runs on Core 0 (dedicated to network + audio pipeline)
//
// Handles: TCP connect, WebSocket upgrade, data receive, audio decode,
// audio pump (WebSdrAudio → PwmAudio), and sending queued commands.
// Core 1 (Arduino loop) handles UI and enqueues tune commands.
//
// This ensures pumpAudio() runs every ~1ms regardless of UI drawing,
// eliminating audio underruns caused by display refresh.
// ---------------------------------------------------------------------------

static void webSdrNetworkTask(void *param)
{
  Serial.printf("WebSDR: network task started on Core %d\n", xPortGetCoreID());

  while (networkTaskRunning)
  {
    if (!state.enabled)
    {
      vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }

    switch (state.connState)
    {
      case WEBSDR_STATE_IDLE:
      {
        state.connState = WEBSDR_STATE_CONNECTING;

        if (doConnect())
        {
          state.connState = WEBSDR_STATE_HANDSHAKE;

          if (doHandshake())
          {
            state.connState = WEBSDR_STATE_STREAMING;
            vTaskDelay(pdMS_TO_TICKS(500));  // Allow server to stabilize
            sendTuneCommand();               // Initial tune (direct, we're on Core 0)
            lastKeepalive = millis();
            audioStarted = false;
            Serial.println("WebSDR: streaming started");
          }
          else
          {
            tcpClient.stop();
            state.reconnectCount++;
            state.connState = WEBSDR_STATE_RECONNECT_WAIT;
            reconnectTime = millis();
          }
        }
        else
        {
          state.connectFailures++;
          state.reconnectCount++;
          state.connState = WEBSDR_STATE_RECONNECT_WAIT;
          reconnectTime = millis();
        }
        break;
      }

      case WEBSDR_STATE_STREAMING:
      {
        if (!tcpClient.connected())
        {
          Serial.println("WebSDR: connection lost");
          doReconnect();
          break;
        }

        // Receive and decode audio data
        doReceive();

        // Pump decoded audio to PWM output
        pumpAudio();

        // Send queued command from Core 1
        taskENTER_CRITICAL(&cmdMux);
        bool hasCmd = queuedCmdReady;
        char cmd[128];
        if (hasCmd)
        {
          memcpy(cmd, queuedCmd, sizeof(cmd));
          queuedCmdReady = false;
        }
        taskEXIT_CRITICAL(&cmdMux);

        if (hasCmd)
        {
          sendCommand(cmd);
          webSdrAudioFlush();
          pwmAudioFlush();
          audioStarted = false;  // Re-prebuffer for clean start
        }

        // Periodic keepalive / diagnostics
        uint32_t now = millis();
        if (now - lastKeepalive > WEBSDR_KEEPALIVE_MS)
        {
          Serial.printf("WebSDR: bytes=%lu buf=%u underruns=%lu overflow=%lu\n",
                         (unsigned long)state.bytesReceived,
                         (unsigned)webSdrAudioAvailable(),
                         (unsigned long)pwmAudioGetUnderrunCount(),
                         (unsigned long)pwmAudioGetOverflowCount());
          lastKeepalive = now;
        }

        // Yield strategy: process fast when data is available, sleep when idle
        if (tcpClient.available() > 0)
          taskYIELD();
        else
          vTaskDelay(1);  // 1ms idle wait
        break;
      }

      case WEBSDR_STATE_RECONNECT_WAIT:
        if (millis() - reconnectTime > WEBSDR_RECONNECT_DELAY_MS)
        {
          webSdrAudioReset();
          audioStarted = false;
          wsUpgraded = false;
          state.connState = WEBSDR_STATE_IDLE;
        }
        else
        {
          vTaskDelay(pdMS_TO_TICKS(50));
        }
        break;

      default:
        vTaskDelay(pdMS_TO_TICKS(100));
        break;
    }
  }

  Serial.println("WebSDR: network task stopped");
  networkTaskHandle = NULL;
  vTaskDelete(NULL);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

bool webSdrInit(void)
{
  memset(&state, 0, sizeof(state));
  state.connState    = WEBSDR_STATE_DISABLED;
  state.selectedServer = 0;
  state.currentBand  = 3;  // Default: 40m band
  state.currentFreq  = 70770;  // Default 7077.0 kHz (0.1 kHz units)
  state.currentMod   = WEBSDR_MOD_LSB;
  return true;
}

void webSdrDeinit(void)
{
  webSdrStop();
  webSdrDisconnect();
  state.connState = WEBSDR_STATE_DISABLED;
}

bool webSdrConnect(void)
{
  // Connection is now handled by the network task on Core 0.
  // Just set state to IDLE so the network task picks it up.
  if (state.connState == WEBSDR_STATE_STREAMING) return true;
  state.connState = WEBSDR_STATE_IDLE;
  return true;
}

void webSdrDisconnect(void)
{
  if (tcpClient.connected())
  {
    tcpClient.stop();
  }

  wsUpgraded = false;

  if (state.connState != WEBSDR_STATE_DISABLED)
    state.connState = WEBSDR_STATE_IDLE;

  Serial.println("WebSDR: disconnected");
}

bool webSdrIsConnected(void)
{
  return state.connState == WEBSDR_STATE_STREAMING && tcpClient.connected();
}

bool webSdrStart(void)
{
  if (!webSdrIsConnected()) return false;
  // Audio is already flowing via the receive path → audio feed → PWM pump
  return true;
}

void webSdrStop(void)
{
  pwmAudioStop();
}

bool webSdrSetBand(int bandIdx)
{
  if (bandIdx < 0 || bandIdx >= (int)BAND_TABLE_COUNT) return false;

  state.currentBand = bandIdx;

  // Clamp frequency to new band limits (band table in kHz, freq in 0.1 kHz)
  if (state.currentFreq < bandTable[bandIdx].minKHz * 10)
    state.currentFreq = bandTable[bandIdx].minKHz * 10;
  if (state.currentFreq > bandTable[bandIdx].maxKHz * 10)
    state.currentFreq = bandTable[bandIdx].maxKHz * 10;

  queueTuneCommand();
  return true;
}

bool webSdrSetFrequency(uint32_t freq)
{
  // Clamp to current band limits (band table in kHz, freq in 0.1 kHz)
  const WebSdrBand *band = &bandTable[state.currentBand];
  if (freq < band->minKHz * 10) freq = band->minKHz * 10;
  if (freq > band->maxKHz * 10) freq = band->maxKHz * 10;

  state.currentFreq = freq;

  // Throttle tune commands to avoid flooding the server
  tunePending = true;
  return true;
}

bool webSdrSetModulation(WebSdrModulation mod)
{
  if (mod >= WEBSDR_MOD_COUNT) return false;

  state.currentMod = mod;

  queueTuneCommand();
  return true;
}

const WebSdrState *webSdrGetState(void)       { return &state; }
WebSdrConnectionState webSdrGetConnectionState(void) { return state.connState; }
int                webSdrGetBand(void)         { return state.currentBand; }
uint32_t           webSdrGetFrequency(void)    { return state.currentFreq; }
WebSdrModulation   webSdrGetModulation(void)   { return state.currentMod; }

const char *webSdrGetModulationName(WebSdrModulation mod)
{
  static const char *displayNames[] = { "AM", "LSB", "USB", "CW", "FM" };
  if (mod >= WEBSDR_MOD_COUNT) return "?";
  return displayNames[mod];
}

int                webSdrGetServerCount(void)  { return SERVER_COUNT; }
const WebSdrServer *webSdrGetServer(int idx)   { return (idx >= 0 && idx < (int)SERVER_COUNT) ? &servers[idx] : NULL; }
int                webSdrGetBandCount(void)    { return BAND_TABLE_COUNT; }
const WebSdrBand   *webSdrGetBandDef(int idx)  { return (idx >= 0 && idx < (int)BAND_TABLE_COUNT) ? &bandTable[idx] : NULL; }

uint32_t webSdrGetReconnectCount(void)      { return state.reconnectCount; }
uint32_t webSdrGetUnknownFrameCount(void)   { return state.unknownFrames; }
uint32_t webSdrGetDecodeErrorCount(void)    { return webSdrAudioGetDecodeErrorCount(); }

// ---------------------------------------------------------------------------
// Task — called from main loop, drives the state machine
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Task — called from Arduino loop on Core 1.
// Lightweight: only handles tune throttle and diagnostic counter updates.
// All network I/O and audio pumping runs on Core 0 via webSdrNetworkTask.
// ---------------------------------------------------------------------------

void webSdrTask(void)
{
  if (!state.enabled) return;

  uint32_t now = millis();

  // Send throttled tune command (queues for Core 0)
  if (tunePending && (now - lastTuneSent >= TUNE_THROTTLE_MS))
  {
    queueTuneCommand();
    tunePending = false;
    lastTuneSent = now;
  }

  // Update diagnostic counters from audio pipeline
  state.unknownFrames = webSdrAudioGetUnknownFrameCount();
  state.audioUnderruns = pwmAudioGetUnderrunCount();
  state.audioOverflows = pwmAudioGetOverflowCount();
}

// ---------------------------------------------------------------------------
// Mode entry / exit
// ---------------------------------------------------------------------------

void webSdrEnterMode(void)
{
  Serial.println("WebSDR: entering mode (dual-core)");

  // Auto-connect to WiFi if not already connected
  if (WiFi.status() != WL_CONNECTED)
  {
    Serial.println("WebSDR: connecting to WiFi...");
    WiFi.mode(WIFI_STA);
    WiFi.begin("Pixel_5449", "1234567890");

    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 10000)
    {
      delay(100);
    }

    if (WiFi.status() == WL_CONNECTED)
    {
      Serial.printf("WebSDR: WiFi connected, IP %s\n", WiFi.localIP().toString().c_str());

      // Disable WiFi power save — prevents 100-300ms receive stalls
      // that drain the audio buffer during modem sleep wake-up
      esp_wifi_set_ps(WIFI_PS_NONE);
      Serial.println("WebSDR: WiFi power save disabled");
    }
    else
    {
      Serial.println("WebSDR: WiFi connection failed");
    }
  }

  // Initialize audio decode pipeline
  webSdrAudioInit();

  // Initialize PWM audio (8kHz default, will adjust when server sends rate)
  // Don't start playback yet — pumpAudio() will start after pre-buffering
  pwmAudioInit(PWM_AUDIO_DEFAULT_SAMPLE_RATE, PWM_AUDIO_DEFAULT_PIN);
  audioStarted = false;
  lastAudioSampleRate = PWM_AUDIO_DEFAULT_SAMPLE_RATE;

  // Mute the SI4732 analog audio to avoid interference
  // The amp stays enabled since PWM audio uses the same amplifier
  rx.setAudioMute(true);

  state.enabled = true;
  state.connState = WEBSDR_STATE_IDLE;

  // Start network task on Core 0 (WiFi/network core)
  // This handles all TCP I/O, audio decode, and audio pump,
  // ensuring pumpAudio() runs every ~1ms regardless of UI activity on Core 1.
  networkTaskRunning = true;
  xTaskCreatePinnedToCore(
    webSdrNetworkTask,
    "webSdrNet",
    8192,                // Stack size (bytes)
    NULL,                // Parameters
    5,                   // Priority (above idle, below WiFi events)
    &networkTaskHandle,
    0                    // Core 0
  );
}

void webSdrExitMode(void)
{
  Serial.println("WebSDR: exiting mode");

  state.enabled = false;

  // Stop network task first (waits for Core 0 task to finish)
  if (networkTaskHandle)
  {
    networkTaskRunning = false;
    uint32_t t0 = millis();
    while (networkTaskHandle && (millis() - t0 < 2000))
    {
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (networkTaskHandle)
    {
      Serial.println("WebSDR: WARNING - network task did not stop, forcing delete");
      vTaskDelete(networkTaskHandle);
      networkTaskHandle = NULL;
    }
  }

  // Now safe to disconnect and cleanup (network task is stopped)
  webSdrStop();
  webSdrDisconnect();
  pwmAudioDeinit();

  state.connState = WEBSDR_STATE_DISABLED;

  // Restore SI4732 analog audio
  rx.setAudioMute(false);
}

bool webSdrIsActive(void)
{
  return state.enabled;
}
