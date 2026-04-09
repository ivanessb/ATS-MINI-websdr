"""
Isolation test: find which component kills the WebSocket connection.
Runs 4 levels:
  1. recv only (no decode)
  2. recv + decode (no audio)  
  3. recv + decode + audio (no GUI)
  4. Full (with tkinter GUI)
"""
import sys
import struct
import threading
import time
import websocket
import pyaudio

HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

# --- JS 32-bit integer helpers ---
def u32(x): return x & 0xFFFFFFFF
def s32(x):
    x = x & 0xFFFFFFFF
    return x if x < 0x80000000 else x - 0x100000000
def asr32(v, shift):
    v = s32(v)
    return v >> shift

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
S_TABLE = [999, 999, 8, 4, 2, 1, 99, 99]


class Decoder:
    def __init__(self):
        self.Ot = 40
        self.Ut = 0
        self.jt = 0
        self.qt = [0]*20
        self.Kt = [0]*20
        self.Qt = 0
        self.total_samples = 0
        self.pcm_lock = threading.Lock()
        self.pcm_buf = bytearray()

    def reset_pred(self):
        self.qt = [0]*20
        self.Kt = [0]*20
        self.Qt = 0

    def emit(self, sample):
        s = s32(sample)
        if s > 32767: s = 32767
        elif s < -32768: s = -32768
        with self.pcm_lock:
            self.pcm_buf.extend(struct.pack('<h', s))
        self.total_samples += 1

    def emit_batch(self, samples):
        """Emit a batch of samples with a single lock acquisition."""
        data = bytearray(len(samples) * 2)
        for i, sample in enumerate(samples):
            s = s32(sample)
            if s > 32767: s = 32767
            elif s < -32768: s = -32768
            struct.pack_into('<h', data, i*2, s)
        with self.pcm_lock:
            self.pcm_buf.extend(data)
        self.total_samples += len(samples)

    def decode_compressed(self, t, n_start, u_start):
        n = n_start
        u = u_start
        jt = self.jt
        f = 12 if (jt & 16) else 14
        Ut = self.Ut
        Ot = self.Ot
        qt = self.qt
        Kt = self.Kt
        Qt = self.Qt
        samples = []

        sample_count = 0
        tlen = len(t)
        while sample_count < 128:
            b0 = t[n] if n < tlen else 0
            b1 = t[n+1] if n+1 < tlen else 0
            b2 = t[n+2] if n+2 < tlen else 0
            b3 = t[n+3] if n+3 < tlen else 0
            w = u32((b0 << 24) | (b1 << 16) | (b2 << 8) | b3)
            d = 0; underscore = 15 - Ut; T = Ot
            w = u32(w << u)
            if w != 0:
                while (w & 0x80000000) == 0 and d < underscore:
                    w = u32(w << 1); d += 1
                if d < underscore:
                    underscore = d; d += 1; w = u32(w << 1)
                else:
                    underscore = (w >> 24) & 0xFF; d += 8; w = u32(w << 8)
            else:
                underscore = (w >> 24) & 0xFF; d += 8; w = u32(w << 8)
            z = 0
            if underscore >= S_TABLE[Ut]: z += 1
            if underscore >= S_TABLE[Ut - 1]: z += 1
            if z > Ut - 1: z = Ut - 1
            S_val = (((w >> 16) & 0xFFFF) >> (17 - Ut)) & (s32(-1 << z) & 0xFFFF)
            S_val += underscore << (Ut - 1)
            sign_bit = 32 - Ut + z
            if w & u32(1 << sign_bit):
                S_val = s32(~(S_val | ((1 << z) - 1)))
            u += d + Ut - z
            while u >= 8: n += 1; u -= 8
            pred_sum = 0
            for i in range(20): pred_sum += qt[i] * Kt[i]
            pred_sum = s32(pred_sum)
            pred_out = pred_sum >> 12 if pred_sum >= 0 else (pred_sum + 4095) >> 12
            T_val = s32(S_val * T + (T >> 1))
            S_scaled = asr32(T_val, 4)
            for i in range(19, 0, -1):
                qt[i] = s32(qt[i] + (-(asr32(qt[i], 7))) + asr32(s32(Kt[i] * S_scaled), f))
                Kt[i] = Kt[i-1]
            qt[0] = s32(qt[0] + (-(asr32(qt[0], 7))) + asr32(s32(Kt[0] * S_scaled), f))
            Kt[0] = s32(pred_out + T_val)
            sample = s32(Kt[0] + asr32(Qt, 4))
            if jt & 16: Qt = 0
            else: Qt = s32(Qt + asr32(s32(Kt[0] << 4), 3))
            samples.append(sample)
            sample_count += 1
        self.qt = qt; self.Kt = Kt; self.Qt = Qt
        if u == 0: n -= 1
        return n, samples

    def decode_message(self, t):
        """Full decode of a binary message. Returns list of samples."""
        all_samples = []
        n = 0
        while n < len(t):
            b = t[n]
            if (b & 0xF0) == 0xF0:
                if n + 1 < len(t): n += 2
                else: n += 1
            elif b == 0x80:
                if n + 128 < len(t):
                    for i in range(128):
                        all_samples.append(ULAW[t[n + 1 + i]])
                    n += 129
                    self.reset_pred()
                else: break
            elif 0x90 <= b <= 0xDF:
                self.Ut = 14 - (b >> 4)
                n, samps = self.decode_compressed(t, n, 4)
                all_samples.extend(samps)
                n += 1
            elif (b & 0x80) == 0:
                n, samps = self.decode_compressed(t, n, 1)
                all_samples.extend(samps)
                n += 1
            elif b == 0x81:
                if n + 2 < len(t): n += 3
                else: break
            elif b == 0x82:
                if n + 2 < len(t):
                    self.Ot = t[n+1] * 256 + t[n+2]
                    n += 3
                else: break
            elif b == 0x83:
                if n + 1 < len(t):
                    self.jt = t[n+1]
                    n += 2
                else: break
            elif b == 0x84:
                all_samples.extend([0]*128)
                self.reset_pred()
                n += 1
            elif b == 0x85:
                if n + 6 < len(t): n += 7
                else: break
            else:
                n += 1
        return all_samples


def run_test(level, duration=30):
    """
    level 1: recv only
    level 2: recv + decode
    level 3: recv + decode + audio
    """
    print(f"\n{'='*60}")
    print(f"TEST LEVEL {level}: " + {
        1: "recv only (no decode)",
        2: "recv + decode (no audio, no emit)",
        3: "recv + decode + emit to buffer (no audio playback)",
        4: "recv + decode + emit + PyAudio playback",
    }[level])
    print(f"{'='*60}")

    decoder = Decoder()
    msg_count = 0
    t0 = time.time()
    max_decode_ms = 0

    pa = None
    stream = None
    if level >= 4:
        pa = pyaudio.PyAudio()
        def audio_cb(in_data, frame_count, time_info, status):
            needed = frame_count * 2
            with decoder.pcm_lock:
                avail = len(decoder.pcm_buf)
                if avail >= needed:
                    data = bytes(decoder.pcm_buf[:needed])
                    del decoder.pcm_buf[:needed]
                else:
                    data = bytes(decoder.pcm_buf) + b'\x00' * (needed - avail)
                    decoder.pcm_buf.clear()
            return (data, pyaudio.paContinue)
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=8000, output=True,
                        frames_per_buffer=2048, stream_callback=audio_cb)
        stream.start_stream()

    def on_message(ws, message):
        nonlocal msg_count, max_decode_ms
        if isinstance(message, str):
            return
        msg_count += 1
        t = message if isinstance(message, (bytes, bytearray)) else bytes(message)

        if level >= 2:
            t1 = time.perf_counter()
            samples = decoder.decode_message(t)
            dt = (time.perf_counter() - t1) * 1000
            if dt > max_decode_ms:
                max_decode_ms = dt

            if level >= 3:
                # Emit samples (with lock)
                decoder.emit_batch(samples)

        if msg_count % 100 == 0:
            elapsed = time.time() - t0
            print(f"  [{elapsed:.1f}s] msg#{msg_count} max_decode={max_decode_ms:.1f}ms samples={decoder.total_samples}")

    def on_open(ws):
        print("  Connected!")
        time.sleep(0.3)
        ws.send("GET /~~param?f=6980&band=1&lo=-4.0&hi=4.0&mode=1")

    closed_event = threading.Event()

    def on_close(ws, code, msg):
        elapsed = time.time() - t0
        print(f"  [{elapsed:.1f}s] CLOSED: code={code} msg={msg} msgs={msg_count}")
        closed_event.set()

    def on_error(ws, err):
        print(f"  [{time.time()-t0:.1f}s] ERROR: {err}")

    ws = websocket.WebSocketApp(
        f"ws://{HOST}:{PORT}/~~stream?v=11",
        on_open=on_open, on_message=on_message,
        on_close=on_close, on_error=on_error,
    )

    ws_thread = threading.Thread(target=lambda: ws.run_forever(ping_interval=0), daemon=True)
    ws_thread.start()

    # Wait for duration or disconnect
    closed_event.wait(timeout=duration)
    elapsed = time.time() - t0

    passed = not closed_event.is_set()
    if passed:
        print(f"  [{elapsed:.1f}s] TEST PASSED - still connected after {duration}s ({msg_count} msgs)")
        ws.close()
    else:
        print(f"  [{elapsed:.1f}s] TEST FAILED - disconnected after {elapsed:.1f}s ({msg_count} msgs)")

    if stream:
        stream.stop_stream()
        stream.close()
    if pa:
        pa.terminate()

    time.sleep(2)  # Gap between tests
    return passed


if __name__ == "__main__":
    for level in [2, 3, 4]:
        result = run_test(level, duration=30)
        if not result:
            print(f"\n*** FAILURE at level {level} - this is the breaking point! ***")
            break
    else:
        print("\n*** ALL TESTS PASSED ***")
