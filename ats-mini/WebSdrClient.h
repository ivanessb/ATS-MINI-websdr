#ifndef WEBSDR_CLIENT_H
#define WEBSDR_CLIENT_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

// ---------------------------------------------------------------------------
// WebSDR Client — Direct firmware client for WebSDR servers
//
// This module implements the network protocol layer for connecting to
// WebSDR servers and controlling tuning, modulation, and band selection.
//
// The protocol is NOT a standard internet radio stream. WebSDR uses a
// custom WebSocket-based protocol with paths like ~~stream and ~~param
// for audio and control data.
//
// PROTOCOL ASSUMPTIONS (websdr.ns0.it:8902):
//   - Initial HTTP GET to "/" returns the main page.
//   - A WebSocket connection is opened to "/~~stream" for audio.
//   - Control commands are sent as text frames:
//       "f=<freqHz>"       — Set frequency in Hz
//       "band=<bandId>"    — Select band
//       "mod=<modeName>"   — Set modulation (am, lsb, usb, cw, fm)
//       "bw=<widthHz>"     — Set passband width (optional)
//   - Binary frames on the stream connection carry audio data.
//   - A keepalive or parameter query may be needed periodically.
//
// These assumptions are based on reverse-engineering and may need
// adjustment. Protocol constants and handlers are isolated in this
// module for easy modification.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Modulation types
// ---------------------------------------------------------------------------

typedef enum
{
  WEBSDR_MOD_AM = 0,
  WEBSDR_MOD_LSB,
  WEBSDR_MOD_USB,
  WEBSDR_MOD_CW,
  WEBSDR_MOD_FM,
  WEBSDR_MOD_COUNT
} WebSdrModulation;

// ---------------------------------------------------------------------------
// Connection state machine
// ---------------------------------------------------------------------------

typedef enum
{
  WEBSDR_STATE_DISABLED = 0,     // Feature not active
  WEBSDR_STATE_IDLE,             // Initialized but not connected
  WEBSDR_STATE_CONNECTING,       // TCP connect in progress
  WEBSDR_STATE_HANDSHAKE,        // WebSocket upgrade handshake
  WEBSDR_STATE_STREAMING,        // Connected, receiving audio
  WEBSDR_STATE_RECONNECT_WAIT,   // Waiting before reconnect attempt
  WEBSDR_STATE_ERROR             // Unrecoverable error (user must exit)
} WebSdrConnectionState;

// ---------------------------------------------------------------------------
// Server and band configuration
// ---------------------------------------------------------------------------

typedef struct
{
  const char *name;       // Display name
  const char *host;       // Hostname
  int         port;       // TCP port
  const char *path;       // Base path (usually "/")
} WebSdrServer;

typedef struct
{
  const char *name;       // Band name for display
  int         bandId;     // Server-side band identifier
  uint32_t    minKHz;     // Minimum frequency (kHz)
  uint32_t    maxKHz;     // Maximum frequency (kHz)
} WebSdrBand;

// ---------------------------------------------------------------------------
// Runtime state (single source of truth)
// ---------------------------------------------------------------------------

typedef struct
{
  bool     enabled;           // Feature is active (user entered WebSDR mode)
  WebSdrConnectionState connState;
  uint8_t  loadingProgress;   // UI progress while connecting (0..100)

  int      selectedServer;    // Index into server table
  int      currentBand;       // Index into band table
  uint32_t currentFreq;       // Frequency in 0.1 kHz units (100 Hz resolution)
  WebSdrModulation currentMod;

  // Diagnostics
  uint32_t reconnectCount;
  uint32_t connectFailures;
  uint32_t audioUnderruns;
  uint32_t audioOverflows;
  uint32_t unknownFrames;
  uint32_t bytesReceived;
} WebSdrState;

#ifdef __cplusplus
extern "C" {
#endif

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

// Initialize the WebSDR client. Must be called once at startup.
bool webSdrInit(void);

// Deinitialize and release resources.
void webSdrDeinit(void);

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

// Connect to the currently selected server.
bool webSdrConnect(void);

// Disconnect from the current server.
void webSdrDisconnect(void);

// Returns true if connected and streaming.
bool webSdrIsConnected(void);

// Start audio streaming (after connect).
bool webSdrStart(void);

// Stop audio streaming.
void webSdrStop(void);

// ---------------------------------------------------------------------------
// Tuning controls — these update internal state AND send commands to server
// ---------------------------------------------------------------------------

bool webSdrSetBand(int bandIdx);
bool webSdrSetFrequency(uint32_t freqKHz);
bool webSdrSetModulation(WebSdrModulation mod);

// ---------------------------------------------------------------------------
// State getters
// ---------------------------------------------------------------------------

const WebSdrState *webSdrGetState(void);
WebSdrConnectionState webSdrGetConnectionState(void);

int                webSdrGetBand(void);
uint32_t           webSdrGetFrequency(void);
WebSdrModulation   webSdrGetModulation(void);
const char        *webSdrGetModulationName(WebSdrModulation mod);

// Server/band table access
int                webSdrGetServerCount(void);
const WebSdrServer *webSdrGetServer(int idx);
int                webSdrGetBandCount(void);
const WebSdrBand   *webSdrGetBandDef(int idx);

// ---------------------------------------------------------------------------
// Task — must be called periodically from the main loop
// ---------------------------------------------------------------------------

// Drives the connection state machine, receives data, feeds audio pipeline.
// Non-blocking. Safe to call every loop iteration.
void webSdrTask(void);

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

uint32_t webSdrGetReconnectCount(void);
uint32_t webSdrGetUnknownFrameCount(void);
uint32_t webSdrGetDecodeErrorCount(void);

// ---------------------------------------------------------------------------
// Mode entry/exit (called from menu system)
// ---------------------------------------------------------------------------

// Enter WebSDR mode: init audio, connect, start streaming.
void webSdrEnterMode(void);

// Exit WebSDR mode: stop streaming, disconnect, restore normal radio.
void webSdrExitMode(void);

// Returns true if WebSDR mode is currently active.
bool webSdrIsActive(void);

// Returns loading status (0..100) while WebSDR is connecting.
uint8_t webSdrGetLoadingProgress(void);

#ifdef __cplusplus
}
#endif

#endif // WEBSDR_CLIENT_H
