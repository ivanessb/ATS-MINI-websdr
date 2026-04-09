"""
WebSDR live audio player v2 - correct implementation of the PA3FWM compressed codec.
Carefully emulates JavaScript 32-bit integer arithmetic.

Usage: python play_websdr2.py [freq_khz] [mode] [band]
"""

import sys
import struct
import threading
import time
import websocket
import pyaudio

try:
    import numpy as np
    from PIL import Image, ImageTk
    HAS_WATERFALL = True
except ImportError:
    HAS_WATERFALL = False

# HOST = "websdr.ns0.it"
# PORT = 8902

# http://websdr.yo3ggx.ro:8765/
HOST = "http://websdr.yo3ggx.ro"
PORT = 8765

#HOST = "sdr.websdrmaasbree.nl"
#PORT = 8901


# --- JS 32-bit integer helpers ---
def u32(x):
    """Unsigned 32-bit mask."""
    return x & 0xFFFFFFFF

def s32(x):
    """Signed 32-bit conversion (like JS |0)."""
    x = x & 0xFFFFFFFF
    return x if x < 0x80000000 else x - 0x100000000

def asr32(v, shift):
    """Arithmetic right shift of signed 32-bit value (like JS >>)."""
    v = s32(v)
    return v >> shift

# Mu-law decode table from websdr-sound.js
ULAW = [
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
]

# Threshold table for z calculation
S_TABLE = [999, 999, 8, 4, 2, 1, 99, 99]

# Waterfall FFT parameters
FFT_SIZE = 1024
WF_WIDTH = FFT_SIZE // 2   # positive frequency bins
WF_HEIGHT = 150

def _make_wf_palette():
    """SDR waterfall palette: black > blue > cyan > green > yellow > red > white"""
    stops = [
        (0.00,   0,   0,   0),
        (0.15,   0,   0, 128),
        (0.30,   0,   0, 255),
        (0.45,   0, 200, 255),
        (0.55,   0, 255,   0),
        (0.70, 255, 255,   0),
        (0.85, 255,   0,   0),
        (1.00, 255, 255, 255),
    ]
    pal = []
    for i in range(256):
        t = i / 255.0
        for j in range(len(stops) - 1):
            if t <= stops[j + 1][0]:
                t0, r0, g0, b0 = stops[j]
                t1, r1, g1, b1 = stops[j + 1]
                f = (t - t0) / (t1 - t0) if t1 > t0 else 0
                pal.append((
                    max(0, min(255, int(r0 + f * (r1 - r0)))),
                    max(0, min(255, int(g0 + f * (g1 - g0)))),
                    max(0, min(255, int(b0 + f * (b1 - b0)))),
                ))
                break
    return pal

WF_PALETTE = _make_wf_palette()
if HAS_WATERFALL:
    WF_PAL_NP = np.array(WF_PALETTE, dtype=np.uint8)
    WF_WINDOW = np.hanning(FFT_SIZE)


class WebSDRPlayer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.ws = None
        self.running = False
        self.bands = None  # Set after fetch_bands()

        # Audio output
        self.sample_rate = 8000
        self.pcm_lock = threading.Lock()
        self.pcm_buf = bytearray()
        self.pa = pyaudio.PyAudio()
        self.stream = None

        # Protocol state
        self.Ot = 40       # quantization parameter
        self.Ut = 0        # shift parameter
        self.jt = 0        # mode info byte
        self.smeter = 0

        # Prediction state (matching JS: qt, Kt, Qt)
        self.qt = [0] * 20
        self.Kt = [0] * 20
        self.Qt = 0

        # Tune throttle (like ESP32's TUNE_THROTTLE_MS = 150)
        self._last_tune_time = 0
        self._last_tune_cmd = None
        self._tune_pending = None   # cmd string to send later
        self._tune_timer = None

        # Auto-reconnect state
        self._freq = None
        self._band = None
        self._mode = None
        self._lo = -4.0
        self._hi = 4.0
        self._should_reconnect = True

        # Stats
        self.msg_count = 0
        self.total_bytes = 0
        self.total_samples = 0
        self.decode_errors = 0

        # Waterfall FFT buffer
        self.wf_lock = threading.Lock()
        self.wf_buf = []

    def start_audio(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=2048,
            stream_callback=self._audio_cb
        )
        self.stream.start_stream()
        print(f"  Audio output: {self.sample_rate} Hz, Int16, mono")

    def _audio_cb(self, in_data, frame_count, time_info, status):
        needed = frame_count * 2
        with self.pcm_lock:
            avail = len(self.pcm_buf)
            if avail >= needed:
                data = bytes(self.pcm_buf[:needed])
                del self.pcm_buf[:needed]
            else:
                data = bytes(self.pcm_buf) + b'\x00' * (needed - avail)
                self.pcm_buf.clear()
        return (data, pyaudio.paContinue)

    def emit(self, sample):
        """Output one Int16 sample (used for mu-law blocks only)."""
        s = s32(sample)
        if s > 32767: s = 32767
        elif s < -32768: s = -32768
        with self.pcm_lock:
            self.pcm_buf.extend(struct.pack('<h', s))
        self.total_samples += 1

    def emit_batch(self, samples):
        """Output a batch of Int16 samples with a single lock acquisition."""
        data = bytearray(len(samples) * 2)
        for i, sample in enumerate(samples):
            s = s32(sample)
            if s > 32767: s = 32767
            elif s < -32768: s = -32768
            struct.pack_into('<h', data, i * 2, s)
        with self.pcm_lock:
            self.pcm_buf.extend(data)
        self.total_samples += len(samples)
        # Feed waterfall FFT buffer
        with self.wf_lock:
            self.wf_buf.extend(samples)
            if len(self.wf_buf) > FFT_SIZE * 4:
                del self.wf_buf[:len(self.wf_buf) - FFT_SIZE * 4]

    def get_fft(self):
        """Return FFT power spectrum in dB for positive frequencies."""
        with self.wf_lock:
            if len(self.wf_buf) < FFT_SIZE:
                return None
            data = self.wf_buf[-FFT_SIZE:]
        arr = np.array(data, dtype=np.float64)
        windowed = arr * WF_WINDOW
        spectrum = np.abs(np.fft.rfft(windowed))
        spectrum = np.maximum(spectrum, 1e-10)
        return 20.0 * np.log10(spectrum[1:])

    def get_passband_fft(self):
        """Return FFT stretched to full window, labeled as ±50 kHz."""
        full = self.get_fft()
        if full is None:
            return None, 0, 0

        freq = self._freq or 0
        span = 50.0  # ±50 kHz
        rf_lo = freq - span
        rf_hi = freq + span

        # Stretch actual data across entire window
        x_old = np.linspace(0, 1, len(full))
        x_new = np.linspace(0, 1, WF_WIDTH)
        resized = np.interp(x_new, x_old, full)

        return resized, rf_lo, rf_hi

    def reset_pred(self):
        """Reset prediction state (on mu-law block or silence)."""
        self.qt = [0] * 20
        self.Kt = [0] * 20
        self.Qt = 0

    def decode_compressed(self, t, n_start, u_start):
        """
        Decode 128 compressed audio samples from byte array t starting at offset n_start
        with initial bit offset u_start.
        Returns (new_byte_offset, list_of_128_samples).

        Exact port of the JS compressed audio decoder from websdr-sound.js.
        All bit ops use 32-bit unsigned arithmetic, matching JS behavior.
        """
        n = n_start
        u = u_start
        jt = self.jt
        f = 12 if (jt & 16) else 14
        Ut = self.Ut
        Ot = self.Ot
        qt = self.qt
        Kt = self.Kt
        Qt = self.Qt

        sample_count = 0
        samples_out = []
        tlen = len(t)
        while sample_count < 128:
            # In JS, reading t[n+x] beyond array returns undefined -> 0 in bitwise ops
            # We must NOT break early; always emit 128 samples
            b0 = t[n] if n < tlen else 0
            b1 = t[n+1] if n+1 < tlen else 0
            b2 = t[n+2] if n+2 < tlen else 0
            b3 = t[n+3] if n+3 < tlen else 0
            w = u32((b0 << 24) | (b1 << 16) | (b2 << 8) | b3)

            d = 0
            underscore = 15 - Ut  # max leading-zero count before overflow
            T = Ot  # quantization step

            # Shift by accumulated bit offset 'u'
            # JS: if (0 != (w <<= u))
            w = u32(w << u)

            if w != 0:
                # Count leading zeros until we hit a '1' or reach max
                while (w & 0x80000000) == 0 and d < underscore:
                    w = u32(w << 1)
                    d += 1

                if d < underscore:
                    # Normal case: found a '1' bit
                    underscore = d  # save leading-zero count
                    d += 1          # consume the '1' bit
                    w = u32(w << 1)
                else:
                    # Overflow: read next 8 bits as literal
                    underscore = (w >> 24) & 0xFF
                    d += 8
                    w = u32(w << 8)
            else:
                # All zeros in the window
                underscore = (w >> 24) & 0xFF  # = 0
                d += 8
                w = u32(w << 8)  # still 0

            # Calculate z (number of extra mantissa bits)
            z = 0
            if underscore >= S_TABLE[Ut]:
                z += 1
            if underscore >= S_TABLE[Ut - 1]:
                z += 1
            if z > Ut - 1:
                z = Ut - 1

            # Extract mantissa bits from w
            # JS: S = ((w>>16 & 65535) >> (17-Ut)) & (-1<<z)
            S_val = (((w >> 16) & 0xFFFF) >> (17 - Ut)) & (s32(-1 << z) & 0xFFFF)
            S_val += underscore << (Ut - 1)

            # Check sign bit
            # JS: if (0 != (w & (1 << (32 - Ut + z))))
            sign_bit = 32 - Ut + z
            if w & u32(1 << sign_bit):
                S_val = s32(~(S_val | ((1 << z) - 1)))

            # Advance bit position
            u += d + Ut - z
            while u >= 8:
                n += 1
                u -= 8

            # Compute prediction: w = sum(qt[i]*Kt[i])
            # JS: for (d=w=0; 20>d; d++) w += qt[d]*Kt[d];
            # JS: w = 0<=(w|=0) ? w>>12 : (w+4095)>>12;
            pred_sum = 0
            for i in range(20):
                pred_sum += qt[i] * Kt[i]
            pred_sum = s32(pred_sum)  # JS |= 0

            if pred_sum >= 0:
                pred_out = pred_sum >> 12
            else:
                pred_out = (pred_sum + 4095) >> 12

            # JS: S = (T = S*T + T/2) >> 4
            # Note: T is Ot, S is S_val
            # T_val = S_val * Ot + Ot/2 (integer division in JS for positive Ot)
            T_val = s32(S_val * T + (T >> 1))  # JS: T/2 with |0 truncation = T>>1 for positive T
            S_scaled = asr32(T_val, 4)

            # Update prediction coefficients
            # JS: for (d=19; 0<=d && (qt[d] += -(qt[d]>>7) + (Kt[d]*S>>f), 0!=d); d--)
            #        Kt[d] = Kt[d-1];
            # Kt[0] = w + T;  (w = pred_out, T = T_val)
            for i in range(19, 0, -1):
                decay = -(asr32(qt[i], 7))
                adapt = asr32(s32(Kt[i] * S_scaled), f)
                qt[i] = s32(qt[i] + decay + adapt)
                Kt[i] = Kt[i - 1]
            decay0 = -(asr32(qt[0], 7))
            adapt0 = asr32(s32(Kt[0] * S_scaled), f)
            qt[0] = s32(qt[0] + decay0 + adapt0)

            Kt[0] = s32(pred_out + T_val)

            # Output sample
            # JS: d = Kt[0] + (Qt>>4);
            # Qt = (16&jt) ? 0 : Qt + (Kt[0]<<4>>3);
            sample = s32(Kt[0] + asr32(Qt, 4))
            if jt & 16:
                Qt = 0
            else:
                Qt = s32(Qt + asr32(s32(Kt[0] << 4), 3))

            samples_out.append(sample)
            sample_count += 1

        self.qt = qt
        self.Kt = Kt
        self.Qt = Qt

        if u == 0:
            n -= 1

        return n, samples_out

    def on_message(self, ws, message):
        if isinstance(message, str):
            print(f"  WS text msg: {message[:100]}")
            return

        t = message if isinstance(message, (bytes, bytearray)) else bytes(message)
        self.total_bytes += len(t)
        self.msg_count += 1

        # Log first few bytes of each binary message periodically
        if self.msg_count <= 5 or self.msg_count % 500 == 0:
            preview = ' '.join(f'{x:02x}' for x in t[:16])
            print(f"  WS bin msg #{self.msg_count}: len={len(t)} first16=[{preview}]")

        n = 0
        all_samples = []
        while n < len(t):
            b = t[n]

            if (b & 0xF0) == 0xF0:
                # S-meter
                if n + 1 < len(t):
                    self.smeter = (b & 0x0F) * 256 + t[n + 1]
                    n += 2
                else:
                    n += 1

            elif b == 0x80:
                # Mu-law block (128 bytes)
                if n + 128 < len(t):
                    for i in range(128):
                        all_samples.append(ULAW[t[n + 1 + i]])
                    n += 129
                    self.reset_pred()
                else:
                    break

            elif 0x90 <= b <= 0xDF:
                # Compressed type A
                self.Ut = 14 - (b >> 4)
                n, samps = self.decode_compressed(t, n, 4)
                all_samples.extend(samps)
                n += 1

            elif (b & 0x80) == 0:
                # Compressed type B
                n, samps = self.decode_compressed(t, n, 1)
                all_samples.extend(samps)
                n += 1

            elif b == 0x81:
                if n + 2 < len(t):
                    new_rate = t[n + 1] * 256 + t[n + 2]
                    if new_rate > 0 and new_rate != self.sample_rate:
                        self.sample_rate = new_rate
                        print(f"  Sample rate changed to {new_rate} Hz")
                        self.start_audio()
                    n += 3
                else:
                    break

            elif b == 0x82:
                if n + 2 < len(t):
                    old_ot = self.Ot
                    self.Ot = t[n + 1] * 256 + t[n + 2]
                    if self.Ot != old_ot:
                        print(f"  Ot changed: {old_ot} -> {self.Ot}")
                    n += 3
                else:
                    break

            elif b == 0x83:
                if n + 1 < len(t):
                    old_jt = self.jt
                    self.jt = t[n + 1]
                    if self.jt != old_jt:
                        print(f"  jt changed: {old_jt} -> {self.jt} (mode bits: {self.jt:#04x})")
                    n += 2
                else:
                    break

            elif b == 0x84:
                all_samples.extend([0] * 128)
                self.reset_pred()
                n += 1

            elif b == 0x85:
                if n + 6 < len(t):
                    n += 7
                else:
                    break

            else:
                n += 1  # skip unknown

        # Batch emit all decoded samples with a single lock acquisition
        if all_samples:
            self.emit_batch(all_samples)

    def on_error(self, ws, error):
        print(f"  WS Error: {error}")

    def on_close(self, ws, code, msg):
        if self._should_reconnect:
            print(f"  WS Closed unexpectedly: code={code} msg={msg} — will reconnect")
        else:
            print(f"  WS Closed (user requested): code={code} msg={msg}")
        self.running = False

    def on_open(self, ws):
        print("  WebSocket connected!")
        self.running = True

    def tune(self, freq, band, mode):
        """Tune to a frequency with throttling (like ESP32's TUNE_THROTTLE_MS=150)."""
        # Save current tune params for auto-reconnect
        self._freq = freq
        self._band = band
        self._mode = mode

        mode_map = {"cw": 0, "lsb": 0, "usb": 0, "am": 1, "fm": 4}
        m = mode_map.get(mode.lower(), 1)
        lo, hi = -4.0, 4.0
        if mode.lower() == "lsb":
            lo, hi = -2.8, -0.3
        elif mode.lower() == "usb":
            lo, hi = 0.3, 2.8
        elif mode.lower() == "am":
            lo, hi = -4.0, 4.0
        elif mode.lower() == "fm":
            lo, hi = -8.0, 8.0

        self._lo = lo
        self._hi = hi

        # Auto-detect correct band from frequency
        if self.bands:
            for i, (name, center, blo, bhi) in enumerate(self.bands):
                if blo <= freq <= bhi:
                    band = i
                    break

        freq_str = f"{int(freq)}" if freq == int(freq) else f"{freq}"
        cmd = f"GET /~~param?f={freq_str}&band={band}&lo={lo}&hi={hi}&mode={m}"

        # Suppress duplicate commands
        if cmd == self._last_tune_cmd:
            return

        # Throttle: min 300ms between tune commands, always coalesce to latest
        now = time.time()
        elapsed = now - self._last_tune_time
        if elapsed < 0.3:
            # Always store the latest; cancel old timer, start new one
            self._tune_pending = cmd
            if self._tune_timer is not None:
                self._tune_timer.cancel()
            delay = 0.3 - elapsed
            self._tune_timer = threading.Timer(delay, self._send_pending_tune)
            self._tune_timer.daemon = True
            self._tune_timer.start()
            return

        self._send_tune(cmd)

    def _send_pending_tune(self):
        """Send the most recent pending tune command after throttle delay."""
        self._tune_timer = None
        cmd = self._tune_pending
        if cmd:
            self._tune_pending = None
            self._send_tune(cmd)

    def _send_tune(self, cmd):
        """Actually send a tune command."""
        self._last_tune_cmd = cmd
        self._last_tune_time = time.time()
        print(f"  Tune: {cmd}")
        if self.ws and self.running:
            try:
                self.ws.send(cmd)
            except Exception as e:
                print(f"  Tune send failed: {e}")

    def connect(self, freq=7106, band=1, mode="lsb"):
        self._freq = freq
        self._band = band
        self._mode = mode
        self.start_audio()
        self._connect_loop(freq, band, mode)

    def _connect_loop(self, freq, band, mode):
        """Connect with auto-reconnect on disconnect."""
        while self._should_reconnect:
            url = f"ws://{self.host}:{self.port}/~~stream?v=11"
            # Use latest tune params if available
            f = self._freq or freq
            b = self._band or band
            m = self._mode or mode
            print(f"Connecting to {url}")
            print(f"Frequency: {f} kHz, Mode: {m}, Band: {b}")

            # Reset protocol state for fresh connection
            self.Ot = 40
            self.Ut = 0
            self.jt = 0
            self.reset_pred()
            self._last_tune_cmd = None

            self.ws = websocket.WebSocketApp(
                url,
                on_open=lambda ws: self._on_open_tune(ws, f, b, m),
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
            )

            stats = threading.Thread(target=self._stats, daemon=True)
            stats.start()

            self.ws.run_forever(ping_interval=0)

            if self._should_reconnect:
                print("  Reconnecting in 2 seconds...")
                time.sleep(2)

    def _on_open_tune(self, ws, freq, band, mode):
        self.on_open(ws)
        time.sleep(0.3)
        self.tune(freq, band, mode)

    def _stats(self):
        t0 = time.time()
        while True:
            time.sleep(10)
            if not self.running:
                break
            dt = time.time() - t0
            with self.pcm_lock:
                buf_ms = len(self.pcm_buf) / 2 / self.sample_rate * 1000
            print(f"  [{dt:.0f}s] msgs={self.msg_count} in={self.total_bytes//1024}KB "
                  f"samples={self.total_samples} buf={buf_ms:.0f}ms "
                  f"smeter={self.smeter} errors={self.decode_errors}")

    def cleanup(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()


def fetch_bands(host, port):
    """Fetch band info from server, return list of (name, centerfreq, lo, hi)."""
    import urllib.request, re, json
    try:
        url = f"http://{host}:{port}/tmp/bandinfo.js"
        r = urllib.request.urlopen(url, timeout=10)
        txt = r.read().decode()
        names = re.findall(r"name:\s*'([^']*)'", txt)
        centers = re.findall(r'centerfreq:\s*([\d.]+)', txt)
        srates = re.findall(r'samplerate:\s*([\d.]+)', txt)
        bands = []
        for i, (n, c, s) in enumerate(zip(names, centers, srates)):
            label = n if n else f"Band {i}"
            cf = float(c)
            sr = float(s)
            bands.append((label, cf, cf - sr / 2, cf + sr / 2))
        return bands
    except Exception as e:
        print(f"  Could not fetch bands: {e}")
        return None


# Fallback bands for sdr.websdrmaasbree.nl
DEFAULT_BANDS = [
    ("80m",  3700.0, 2676.0, 4724.0),
    ("40m",  6980.0, 5956.0, 8004.0),
    ("20m", 14100.0, 13076.0, 15124.0),
    ("10m", 27975.0, 26951.0, 28999.0),
    ("6m",  51000.0, 49976.0, 52024.0),
    ("2m", 145000.0, 143976.0, 146024.0),
    ("70cm", 432990.0, 431966.0, 434014.0),
]

MODES = ["AM", "LSB", "USB", "CW", "FM"]


def main():
    import tkinter as tk

    print(f"=== WebSDR Player v2 - {HOST}:{PORT} ===")
    print("Fetching band info...")
    bands = fetch_bands(HOST, PORT)
    if not bands:
        bands = DEFAULT_BANDS
        print("  Using default band list")
    else:
        print(f"  Found {len(bands)} bands: {[b[0] for b in bands]}")

    player = WebSDRPlayer(HOST, PORT)
    player.bands = bands

    # Log band ranges for debugging
    for i, (name, center, blo, bhi) in enumerate(bands):
        print(f"  Band {i}: {name} center={center:.0f} range={blo:.0f}-{bhi:.0f} kHz")

    # --- Connect in background thread ---
    def ws_thread():
        f0 = 6980
        b0 = 1
        m0 = "lsb"
        player.connect(f0, b0, m0)

    t = threading.Thread(target=ws_thread, daemon=True)
    t.start()

    # --- Build GUI ---
    root = tk.Tk()
    root.title(f"WebSDR Player - {HOST}:{PORT}")
    root.configure(bg="#2b2b2b")
    root.resizable(True, True)

    style_label = {"bg": "#2b2b2b", "fg": "#e0e0e0", "font": ("Consolas", 10)}
    style_entry = {"bg": "#3c3c3c", "fg": "#00ff88", "font": ("Consolas", 12),
                   "insertbackground": "#00ff88", "relief": "flat", "bd": 2}
    style_btn = {"bg": "#404040", "fg": "#f0c080", "font": ("Consolas", 10),
                 "activebackground": "#555555", "activeforeground": "#ffffff",
                 "relief": "raised", "bd": 1, "padx": 6, "pady": 3}
    style_band_btn = {"bg": "#353535", "fg": "#80c0ff", "font": ("Consolas", 9),
                      "activebackground": "#555555", "activeforeground": "#ffffff",
                      "relief": "raised", "bd": 1, "padx": 4, "pady": 2}

    status_var = tk.StringVar(value="Connecting...")
    cur_freq = tk.StringVar(value="6980")
    cur_mode = tk.StringVar(value="LSB")
    cur_band = tk.IntVar(value=1)

    # --- Status bar ---
    tk.Label(root, textvariable=status_var, bg="#1e1e1e", fg="#00ff88",
             font=("Consolas", 9), anchor="w", padx=5).pack(fill="x")

    # --- Frequency frame ---
    freq_frame = tk.Frame(root, bg="#2b2b2b", padx=10, pady=5)
    freq_frame.pack(fill="x")
    tk.Label(freq_frame, text="Freq (kHz):", **style_label).pack(side="left")
    freq_entry = tk.Entry(freq_frame, textvariable=cur_freq, width=12, **style_entry)
    freq_entry.pack(side="left", padx=5)

    def do_tune(*_args):
        try:
            f = float(cur_freq.get())
        except ValueError:
            status_var.set("Invalid frequency!")
            return
        b = cur_band.get()
        m = cur_mode.get().lower()
        status_var.set(f"Tuning: {f} kHz, {m.upper()}, band {b}")
        player.tune(f, b, m)

    tk.Button(freq_frame, text="Tune", command=do_tune, **style_btn).pack(side="left", padx=5)
    freq_entry.bind("<Return>", do_tune)

    # --- Mode frame ---
    mode_frame = tk.LabelFrame(root, text="Mode", bg="#2b2b2b", fg="#aaaaaa",
                               font=("Consolas", 9), padx=5, pady=3)
    mode_frame.pack(fill="x", padx=10, pady=3)

    def set_mode(m):
        cur_mode.set(m)
        do_tune()

    for m in MODES:
        tk.Button(mode_frame, text=m, width=5,
                  command=lambda m=m: set_mode(m), **style_btn).pack(side="left", padx=2)

    # --- Band frame ---
    band_frame = tk.LabelFrame(root, text="Band", bg="#2b2b2b", fg="#aaaaaa",
                               font=("Consolas", 9), padx=5, pady=3)
    band_frame.pack(fill="x", padx=10, pady=3)

    def set_band(idx, center):
        cur_band.set(idx)
        cur_freq.set(str(center))
        do_tune()

    for i, (name, center, blo, bhi) in enumerate(bands):
        tk.Button(band_frame, text=f"{name}\n{center:.0f}",
                  command=lambda i=i, c=center: set_band(i, c),
                  **style_band_btn).pack(side="left", padx=2, pady=2)

    # --- Presets frame ---
    preset_frame = tk.LabelFrame(root, text="Presets", bg="#2b2b2b", fg="#aaaaaa",
                                 font=("Consolas", 9), padx=5, pady=3)
    preset_frame.pack(fill="x", padx=10, pady=3)

    presets = [
        ("BBC WS",   5875, "am",  1),
        ("40m SSB",  7106, "lsb", 1),
        ("20m SSB", 14200, "usb", 2),
        ("49m SW",   5950, "am",  1),
        ("80m LSB",  3630, "lsb", 0),
        ("10m FM",  27500, "fm",  3),
    ]

    def apply_preset(f, m, b):
        cur_freq.set(str(f))
        cur_mode.set(m.upper())
        cur_band.set(b)
        do_tune()

    for label, f, m, b in presets:
        tk.Button(preset_frame, text=f"{label}\n{f} {m.upper()}",
                  command=lambda f=f, m=m, b=b: apply_preset(f, m, b),
                  **style_band_btn).pack(side="left", padx=2, pady=2)

    # --- Waterfall ---
    if HAS_WATERFALL:
        print("  Waterfall: enabled")
        wf_frame = tk.LabelFrame(root, text="Waterfall", bg="#2b2b2b", fg="#aaaaaa",
                                  font=("Consolas", 9), padx=5, pady=3)
        wf_frame.pack(fill="x", padx=10, pady=3)

        wf_canvas = tk.Canvas(wf_frame, width=WF_WIDTH, height=WF_HEIGHT + 15,
                               bg="black", highlightthickness=0)
        wf_canvas.pack()

        wf_rgb = np.zeros((WF_HEIGHT, WF_WIDTH, 3), dtype=np.uint8)
        pil_img = Image.fromarray(wf_rgb, 'RGB')
        wf_photo = ImageTk.PhotoImage(pil_img)
        wf_canvas_item = wf_canvas.create_image(0, 0, anchor="nw", image=wf_photo)
        wf_img_ref = [wf_photo]

        # Frequency axis labels (RF kHz) — updated on tune
        wf_freq_labels = []
        for frac in [0, 0.25, 0.5, 0.75, 1.0]:
            x = int(frac * (WF_WIDTH - 1))
            anc = "nw" if frac == 0 else ("ne" if frac == 1.0 else "n")
            lbl = wf_canvas.create_text(x, WF_HEIGHT + 2, text="",
                                         fill="#888888", font=("Consolas", 7), anchor=anc)
            wf_freq_labels.append((frac, lbl))

        wf_rf_range = [0.0, 0.0]  # current RF range shown

        def update_wf_labels():
            try:
                rf_lo, rf_hi = wf_rf_range
                for frac, lbl_id in wf_freq_labels:
                    rf = rf_lo + frac * (rf_hi - rf_lo)
                    wf_canvas.itemconfig(lbl_id, text=f"{rf:.1f}")
            except Exception as e:
                print(f"  WF label error: {e}")

        wf_db_floor = [None]  # auto-scale tracking
        wf_db_ceil = [None]

        def update_waterfall():
            try:
                db, rf_lo, rf_hi = player.get_passband_fft()
                if db is not None:
                    wf_rf_range[0] = rf_lo
                    wf_rf_range[1] = rf_hi
                    # Percentile-based scaling: noise floor at 10th pct, ceiling at 99th pct
                    # This keeps noise dark and makes signals pop
                    p10 = float(np.percentile(db, 10))
                    p99 = float(np.percentile(db, 99))
                    if wf_db_floor[0] is None:
                        wf_db_floor[0] = p10
                        wf_db_ceil[0] = p99
                    else:
                        wf_db_floor[0] = wf_db_floor[0] * 0.93 + p10 * 0.07
                        wf_db_ceil[0] = wf_db_ceil[0] * 0.93 + p99 * 0.07
                    floor = wf_db_floor[0] - 3  # slight padding below noise
                    ceil = wf_db_ceil[0] + 10    # headroom above peaks
                    span = max(ceil - floor, 15)
                    normalized = np.clip((db - floor) / span * 255, 0, 255).astype(np.uint8)
                    wf_rgb[1:] = wf_rgb[:-1]
                    wf_rgb[0] = WF_PAL_NP[normalized[:WF_WIDTH]]
                    pil_img = Image.fromarray(wf_rgb, 'RGB')
                    photo = ImageTk.PhotoImage(pil_img)
                    wf_canvas.itemconfig(wf_canvas_item, image=photo)
                    wf_img_ref[0] = photo
                update_wf_labels()
            except Exception as e:
                print(f"  WF error: {e}")
            root.after(100, update_waterfall)

        root.after(1500, update_waterfall)

    # --- Stats update ---
    def update_stats():
        if player.running:
            with player.pcm_lock:
                buf_ms = len(player.pcm_buf) / 2 / player.sample_rate * 1000
            status_var.set(
                f"Connected | smeter={player.smeter} | "
                f"buf={buf_ms:.0f}ms | samples={player.total_samples} | "
                f"in={player.total_bytes//1024}KB"
            )
        else:
            status_var.set("Reconnecting...")
        root.after(1000, update_stats)

    root.after(2000, update_stats)

    def on_close_window():
        player._should_reconnect = False
        player.running = False
        if player.ws:
            player.ws.close()
        player.cleanup()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close_window)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        on_close_window()


if __name__ == "__main__":
    main()
