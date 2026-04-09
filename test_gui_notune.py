"""Test: does the FULL script (with tkinter GUI + audio) stay connected if we DON'T tune?"""
import sys
import struct
import threading
import time
import websocket
import pyaudio

HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

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

# Minimal player with full decode + audio + tkinter
class TestPlayer:
    def __init__(self):
        self.running = False
        self.pcm_lock = threading.Lock()
        self.pcm_buf = bytearray()
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.Ot = 40; self.Ut = 0; self.jt = 0
        self.qt = [0]*20; self.Kt = [0]*20; self.Qt = 0
        self.msg_count = 0; self.total_samples = 0; self.smeter = 0; self.total_bytes = 0

    def start_audio(self):
        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=8000, output=True,
            frames_per_buffer=2048, stream_callback=self._audio_cb)
        self.stream.start_stream()

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
        s = s32(sample)
        if s > 32767: s = 32767
        elif s < -32768: s = -32768
        with self.pcm_lock:
            self.pcm_buf.extend(struct.pack('<h', s))
        self.total_samples += 1

    def reset_pred(self):
        self.qt = [0]*20; self.Kt = [0]*20; self.Qt = 0

    def decode_compressed(self, t, n_start, u_start):
        n = n_start; u = u_start; jt = self.jt
        f = 12 if (jt & 16) else 14
        Ut = self.Ut; Ot = self.Ot; qt = self.qt; Kt = self.Kt; Qt = self.Qt
        tlen = len(t)
        for _ in range(128):
            b0 = t[n] if n < tlen else 0; b1 = t[n+1] if n+1 < tlen else 0
            b2 = t[n+2] if n+2 < tlen else 0; b3 = t[n+3] if n+3 < tlen else 0
            w = u32((b0 << 24) | (b1 << 16) | (b2 << 8) | b3)
            d = 0; underscore = 15 - Ut; T = Ot
            w = u32(w << u)
            if w != 0:
                while (w & 0x80000000) == 0 and d < underscore: w = u32(w << 1); d += 1
                if d < underscore: underscore = d; d += 1; w = u32(w << 1)
                else: underscore = (w >> 24) & 0xFF; d += 8; w = u32(w << 8)
            else: underscore = (w >> 24) & 0xFF; d += 8; w = u32(w << 8)
            z = 0
            if underscore >= S_TABLE[Ut]: z += 1
            if underscore >= S_TABLE[Ut - 1]: z += 1
            if z > Ut - 1: z = Ut - 1
            S_val = (((w >> 16) & 0xFFFF) >> (17 - Ut)) & (s32(-1 << z) & 0xFFFF)
            S_val += underscore << (Ut - 1)
            sign_bit = 32 - Ut + z
            if w & u32(1 << sign_bit): S_val = s32(~(S_val | ((1 << z) - 1)))
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
            self.emit(sample)
        self.qt = qt; self.Kt = Kt; self.Qt = Qt
        if u == 0: n -= 1
        return n

    def on_message(self, ws, message):
        if isinstance(message, str): return
        t = message if isinstance(message, (bytes, bytearray)) else bytes(message)
        self.total_bytes += len(t)
        self.msg_count += 1
        n = 0
        while n < len(t):
            b = t[n]
            if (b & 0xF0) == 0xF0:
                if n + 1 < len(t): self.smeter = (b & 0x0F) * 256 + t[n + 1]; n += 2
                else: n += 1
            elif b == 0x80:
                if n + 128 < len(t):
                    for i in range(128): self.emit(ULAW[t[n + 1 + i]])
                    n += 129; self.reset_pred()
                else: break
            elif 0x90 <= b <= 0xDF:
                self.Ut = 14 - (b >> 4)
                n = self.decode_compressed(t, n, 4); n += 1
            elif (b & 0x80) == 0:
                n = self.decode_compressed(t, n, 1); n += 1
            elif b == 0x81:
                if n + 2 < len(t): n += 3
                else: break
            elif b == 0x82:
                if n + 2 < len(t): self.Ot = t[n+1]*256 + t[n+2]; n += 3
                else: break
            elif b == 0x83:
                if n + 1 < len(t): self.jt = t[n+1]; n += 2
                else: break
            elif b == 0x84:
                for _ in range(128): self.emit(0)
                self.reset_pred(); n += 1
            elif b == 0x85:
                if n + 6 < len(t): n += 7
                else: break
            else: n += 1


def main():
    import tkinter as tk

    player = TestPlayer()
    player.start_audio()
    t0 = time.time()

    def on_open(ws):
        player.running = True
        print(f"  Connected!")
        time.sleep(0.3)
        ws.send("GET /~~param?f=6980&band=3&lo=-2.8&hi=-0.3&mode=0")
        print(f"  Tune sent (initial only, NO further tunes)")

    def on_close(ws, code, msg):
        print(f"  [{time.time()-t0:.1f}s] CLOSED: code={code} msg={msg} msgs={player.msg_count}")
        player.running = False

    def on_error(ws, err):
        print(f"  [{time.time()-t0:.1f}s] ERROR: {err}")

    ws = websocket.WebSocketApp(
        f"ws://{HOST}:{PORT}/~~stream?v=11",
        on_open=on_open,
        on_message=player.on_message,
        on_close=on_close,
        on_error=on_error,
    )

    ws_thread = threading.Thread(target=lambda: ws.run_forever(ping_interval=0), daemon=True)
    ws_thread.start()

    # Build minimal tkinter GUI (to test if tkinter causes issues)
    root = tk.Tk()
    root.title("WebSDR GUI test - NO TUNING")
    root.configure(bg="#2b2b2b")

    status_var = tk.StringVar(value="Running with GUI, decode, audio - NO tuning happening")
    tk.Label(root, textvariable=status_var, bg="#1e1e1e", fg="#00ff88",
             font=("Consolas", 10)).pack(fill="x", padx=5, pady=5)

    def update_stats():
        if player.running:
            elapsed = time.time() - t0
            with player.pcm_lock:
                buf_ms = len(player.pcm_buf) / 2 / 8000 * 1000
            status_var.set(
                f"[{elapsed:.0f}s] msgs={player.msg_count} "
                f"samples={player.total_samples} buf={buf_ms:.0f}ms "
                f"smeter={player.smeter}")
        else:
            status_var.set(f"DISCONNECTED at {time.time()-t0:.1f}s after {player.msg_count} msgs")
        root.after(1000, update_stats)

    root.after(1000, update_stats)

    def on_close_window():
        player.running = False
        ws.close()
        if player.stream:
            player.stream.stop_stream()
            player.stream.close()
        player.pa.terminate()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close_window)
    print("Running with GUI + decode + audio + NO tuning. Watch for disconnect...")
    root.mainloop()


if __name__ == "__main__":
    main()
