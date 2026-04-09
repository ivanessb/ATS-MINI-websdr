"""
WebSDR live audio player v2 - correct implementation of the PA3FWM compressed codec.
Carefully emulates JavaScript 32-bit integer arithmetic.

Usage: python play_websdr2.py [freq_khz] [mode] [band]
"""

import sys
import struct
import threading
import time
import queue
import websocket
import pyaudio

# HOST = "websdr.ns0.it"
# PORT = 8902
HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

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
        self.pending_tune = None  # Deferred tune args
        self.tune_timer = None
        self.last_tune_cmd = None  # Dedup identical tunes
        self._msg_samples = []    # Per-message sample accumulator

        # Connection tracking
        self.connect_count = 0
        self.connect_time = None

        # Prediction state (matching JS: qt, Kt, Qt)
        self.qt = [0] * 20
        self.Kt = [0] * 20
        self.Qt = 0

        # Stats
        self.msg_count = 0
        self.total_bytes = 0
        self.total_samples = 0
        self.decode_errors = 0

        # Decode queue: recv thread enqueues, decoder thread processes
        self._raw_queue = queue.Queue(maxsize=2000)
        threading.Thread(target=self._decode_loop, daemon=True).start()

    def start_audio(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=256,
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
        """Accumulate one Int16 sample (flushed at end of on_message)."""
        s = s32(sample)
        if s > 32767: s = 32767
        elif s < -32768: s = -32768
        self._msg_samples.append(s)
        self.total_samples += 1

    def reset_pred(self):
        """Reset prediction state (on mu-law block or silence)."""
        self.qt = [0] * 20
        self.Kt = [0] * 20
        self.Qt = 0

    def decode_compressed(self, t, n_start, u_start):
        """
        Decode 128 compressed audio samples from byte array t starting at offset n_start
        with initial bit offset u_start.
        Returns the new byte offset after decoding.

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
        tlen = len(t)
        while sample_count < 128:
            # In JS, reading t[n+x] beyond array returns undefined → 0 in bitwise ops
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

            self.emit(sample)
            sample_count += 1

        self.qt = qt
        self.Kt = Kt
        self.Qt = Qt

        if u == 0:
            n -= 1

        return n

    def on_message(self, ws, message):
        """Receive callback — runs on WebSocket recv thread. Must return fast."""
        if isinstance(message, str):
            print(f"  WS text msg: {message[:100]}")
            return
        try:
            self._raw_queue.put_nowait(message)
        except queue.Full:
            # Decoder can't keep up — flush and reset
            while not self._raw_queue.empty():
                try: self._raw_queue.get_nowait()
                except queue.Empty: break
            self.reset_pred()
            with self.pcm_lock:
                self.pcm_buf.clear()

    def _decode_loop(self):
        """Decoder thread — pulls raw binary messages from queue and decodes."""
        while True:
            try:
                msg = self._raw_queue.get(timeout=5)
            except queue.Empty:
                continue
            try:
                self._decode_binary(msg)
            except Exception as e:
                self.decode_errors += 1
                if self.decode_errors <= 10:
                    print(f"  Decode error #{self.decode_errors}: {e}")

    def _decode_binary(self, message):
        """Decode a binary WebSocket message (runs on decoder thread)."""
        t = message if isinstance(message, (bytes, bytearray)) else bytes(message)
        self.total_bytes += len(t)
        self.msg_count += 1
        self._msg_samples = []

        # Log first few bytes of each binary message periodically
        if self.msg_count <= 5 or self.msg_count % 500 == 0:
            preview = ' '.join(f'{x:02x}' for x in t[:16])
            print(f"  WS bin msg #{self.msg_count}: len={len(t)} first16=[{preview}]")

        n = 0
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
                        self.emit(ULAW[t[n + 1 + i]])
                    n += 129
                    self.reset_pred()
                else:
                    break

            elif 0x90 <= b <= 0xDF:
                # Compressed type A: tag byte's low 4 bits are data
                # DON'T skip tag byte - decoder reads from t[n] with 4-bit offset
                self.Ut = 14 - (b >> 4)
                n = self.decode_compressed(t, n, 4)
                n += 1

            elif (b & 0x80) == 0:
                # Compressed type B
                n = self.decode_compressed(t, n, 1)
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
                for _ in range(128):
                    self.emit(0)
                self.reset_pred()
                n += 1

            elif b == 0x85:
                if n + 6 < len(t):
                    n += 7
                else:
                    break

            else:
                n += 1  # skip unknown

        # Batch write decoded samples to PCM buffer
        if self._msg_samples:
            data = struct.pack(f'<{len(self._msg_samples)}h', *self._msg_samples)
            with self.pcm_lock:
                self.pcm_buf.extend(data)
                # Cap buffer at 2 seconds of audio
                max_bytes = self.sample_rate * 2 * 2
                if len(self.pcm_buf) > max_bytes:
                    del self.pcm_buf[:len(self.pcm_buf) - max_bytes]

    def on_error(self, ws, error):
        print(f"  WS Error: {error}")

    def on_close(self, ws, code, msg):
        duration = time.time() - self.connect_time if self.connect_time else 0
        print(f"  WS Closed: code={code} msg={msg} (connected {duration:.1f}s)")
        self.running = False
        self.last_tune_cmd = None  # Allow re-tune after reconnect

    def on_open(self, ws):
        self.connect_count += 1
        self.connect_time = time.time()
        print(f"  WebSocket connected! (#{self.connect_count})")
        self.running = True
        # Reset protocol state for new connection
        self.Ot = 40
        self.Ut = 0
        self.jt = 0
        self.reset_pred()
        # Flush stale data from previous connection
        while not self._raw_queue.empty():
            try: self._raw_queue.get_nowait()
            except queue.Empty: break

    def tune(self, freq, band, mode):
        """Tune with debounce — coalesces rapid calls, sends only once after 200ms quiet."""
        self.pending_tune = (freq, band, mode)
        # Cancel any existing timer and restart the debounce window
        if self.tune_timer:
            self.tune_timer.cancel()
        self.tune_timer = threading.Timer(0.2, self._fire_pending_tune)
        self.tune_timer.daemon = True
        self.tune_timer.start()

    def _fire_pending_tune(self):
        """Fire the most recent pending tune after debounce delay."""
        self.tune_timer = None
        if self.pending_tune:
            args = self.pending_tune
            self.pending_tune = None
            self._do_tune(*args)

    def _do_tune(self, freq, band, mode):
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

        # Auto-detect correct band from frequency
        if self.bands:
            for i, (name, center, blo, bhi) in enumerate(self.bands):
                if blo <= freq <= bhi:
                    band = i
                    break

        # Format freq as integer if it's a whole number (server expects no .0)
        freq_str = f"{int(freq)}" if freq == int(freq) else f"{freq}"
        cmd = f"GET /~~param?f={freq_str}&band={band}&lo={lo}&hi={hi}&mode={m}&name="

        # Skip if identical to last sent command
        if cmd == self.last_tune_cmd:
            return  # Silently skip duplicates
        self.last_tune_cmd = cmd

        print(f"  Tune: {cmd}")
        if self.ws and self.running:
            try:
                self.ws.send(cmd)
            except (websocket.WebSocketConnectionClosedException, BrokenPipeError, OSError):
                return  # Connection lost, will reconnect
            # Flush old audio data
            while not self._raw_queue.empty():
                try: self._raw_queue.get_nowait()
                except queue.Empty: break
            self.reset_pred()
            with self.pcm_lock:
                self.pcm_buf.clear()
        else:
            print(f"  WARNING: not connected")

    def connect(self, freq=7106, band=1, mode="lsb"):
        url = f"ws://{self.host}:{self.port}/~~stream?v=11"
        print(f"Connecting to {url}")
        print(f"Frequency: {freq} kHz, Mode: {mode}, Band: {band}")

        self.start_audio()

        self.ws = websocket.WebSocketApp(
            url,
            on_open=lambda ws: self._on_open_tune(ws, freq, band, mode),
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )

        stats = threading.Thread(target=self._stats, daemon=True)
        stats.start()

        # No ping/pong - server doesn't support it
        self.ws.run_forever(ping_interval=0)

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

    # --- Connect in background thread with auto-reconnect ---
    def ws_thread():
        player.start_audio()
        while True:
            url = f"ws://{player.host}:{player.port}/~~stream?v=11"
            print(f"Connecting to {url}")
            ws = None
            try:
                ws = websocket.create_connection(
                    url,
                    timeout=10,
                    enable_multithread=True,
                    skip_utf8_validation=True,
                )
                player.ws = ws
                player.on_open(ws)

                time.sleep(0.3)
                f = float(cur_freq.get())
                b = cur_band.get()
                m = cur_mode.get().lower()
                player._do_tune(f, b, m)

                while player.running:
                    try:
                        data = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not data:
                        break
                    player.on_message(ws, data)

            except websocket.WebSocketConnectionClosedException:
                pass
            except Exception as e:
                print(f"  WS Error: {type(e).__name__}: {e}")

            duration = time.time() - player.connect_time if player.connect_time else 0
            print(f"  Disconnected (was connected {duration:.1f}s)")
            player.running = False
            player.last_tune_cmd = None
            if ws:
                try: ws.close()
                except: pass
            print("  Reconnecting in 1s...")
            time.sleep(1)

    t = threading.Thread(target=ws_thread, daemon=True)
    t.start()

    # --- Build GUI ---
    root = tk.Tk()
    root.title(f"WebSDR Player - {HOST}:{PORT}")
    root.configure(bg="#2b2b2b")
    root.resizable(False, False)

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

    # --- Stats update ---
    def update_stats():
        if player.running:
            with player.pcm_lock:
                buf_ms = len(player.pcm_buf) / 2 / player.sample_rate * 1000
            uptime = time.time() - player.connect_time if player.connect_time else 0
            status_var.set(
                f"Connected #{player.connect_count} ({uptime:.0f}s) | "
                f"smeter={player.smeter} | buf={buf_ms:.0f}ms | "
                f"in={player.total_bytes//1024}KB"
            )
        else:
            status_var.set("Disconnected \u2014 reconnecting...")
        root.after(1000, update_stats)

    root.after(2000, update_stats)

    def on_close_window():
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
