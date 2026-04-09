"""
WebSDR live audio player - connects via WebSocket to play audio through PC speakers.
Implements the PA3FWM WebSDR binary protocol reverse-engineered from websdr-sound.js.

Usage: python play_websdr.py [freq_khz] [mode] [band]
Example: python play_websdr.py 7106 lsb 1
         python play_websdr.py 14100 am 2
"""

import sys
import struct
import threading
import time
import wave
import io

try:
    import websocket
except ImportError:
    print("Installing websocket-client...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client"])
    import websocket

try:
    import pyaudio
except ImportError:
    print("Installing pyaudio...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyaudio"])
    import pyaudio

# --- Protocol constants ---
HOST = "websdr.ns0.it"
PORT = 8902

# Mu-law decode table (from websdr-sound.js, variable 'x')
# Converts unsigned byte -> Int16 PCM
ULAW_TABLE = [
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

class WebSDRPlayer:
    def __init__(self, host=HOST, port=PORT):
        self.host = host
        self.port = port
        self.ws = None
        self.running = False
        
        # Audio state
        self.sample_rate = 8000
        self.pcm_buffer = bytearray()
        self.buffer_lock = threading.Lock()
        
        # Protocol state
        self.smeter = 0
        self.Ot = 0       # quantization parameter
        self.Ut = 0        # shift parameter
        self.jt = 0        # mode info
        self.true_freq = 0
        
        # Prediction state for compressed audio
        self.qt = [0] * 20   # prediction coefficients
        self.Kt = [0] * 20   # prediction history
        self.Qt = 0           # DC offset accumulator
        
        # Stats
        self.total_samples = 0
        self.total_bytes_in = 0
        self.msg_count = 0
        self.ulaw_blocks = 0
        self.compressed_blocks = 0
        self.silence_blocks = 0
        
        # PyAudio
        self.pa = pyaudio.PyAudio()
        self.stream = None
        
    def start_audio(self):
        """Start the PyAudio output stream."""
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=1024,
            stream_callback=self._audio_callback
        )
        self.stream.start_stream()
        print(f"Audio output started at {self.sample_rate} Hz")
    
    def _audio_callback(self, in_data, frame_count, time_info, status):
        """PyAudio callback - pull samples from buffer."""
        needed = frame_count * 2  # 2 bytes per Int16 sample
        with self.buffer_lock:
            if len(self.pcm_buffer) >= needed:
                data = bytes(self.pcm_buffer[:needed])
                del self.pcm_buffer[:needed]
            else:
                # Underrun - pad with silence
                available = len(self.pcm_buffer)
                data = bytes(self.pcm_buffer[:available])
                data += b'\x00' * (needed - available)
                self.pcm_buffer.clear()
        return (data, pyaudio.paContinue)
    
    def add_sample(self, s16):
        """Add a single Int16 sample to the output buffer."""
        # Clamp to Int16 range
        if s16 > 32767: s16 = 32767
        elif s16 < -32768: s16 = -32768
        with self.buffer_lock:
            self.pcm_buffer.extend(struct.pack('<h', int(s16)))
        self.total_samples += 1
    
    def decode_ulaw_block(self, data, offset):
        """Decode 128 mu-law bytes starting at offset."""
        for i in range(128):
            b = data[offset + 1 + i]
            self.add_sample(ULAW_TABLE[b])
        self.ulaw_blocks += 1
        return 128  # consumed 128 data bytes (tag already consumed)
    
    def decode_silence_block(self):
        """Insert 128 zero samples."""
        for _ in range(128):
            self.add_sample(0)
        self.silence_blocks += 1
        # Reset prediction state
        self.qt = [0] * 20
        self.Kt = [0] * 20
        self.Qt = 0
    
    def decode_compressed_block(self, data, n, u_init, Ut):
        """
        Decode compressed audio block from the stream.
        This is a predictive coder with entropy coding.
        Returns the new offset n.
        """
        self.compressed_blocks += 1
        u = u_init  # bit offset within current byte group
        o_count = 0
        jt = self.jt
        f_val = 12 if (16 & jt) else 14
        
        while o_count < 128:
            # Read 4 bytes as big-endian uint32
            if n + 3 >= len(data):
                break
            w = ((data[n] & 0xFF) << 24) | ((data[n+1] & 0xFF) << 16) | \
                ((data[n+2] & 0xFF) << 8) | (data[n+3] & 0xFF)
            
            d = 0
            max_d = 15 - Ut
            
            S_table = [999, 999, 8, 4, 2, 1, 99, 99]
            
            if (w << u) & 0xFFFFFFFF != 0:
                shifted = (w << u) & 0xFFFFFFFF
                while (shifted & 0x80000000) == 0 and d < max_d:
                    shifted = (shifted << 1) & 0xFFFFFFFF
                    d += 1
                
                if d < max_d:
                    saved_d = d
                    d += 1
                    shifted = (shifted << 1) & 0xFFFFFFFF
                else:
                    saved_d = (shifted >> 24) & 0xFF
                    d += 8
                    shifted = (shifted << 8) & 0xFFFFFFFF
            else:
                saved_d = (((w << u) & 0xFFFFFFFF) >> 24) & 0xFF
                d += 8
                shifted = (((w << u) & 0xFFFFFFFF) << 8) & 0xFFFFFFFF
            
            z = 0
            if Ut >= 2 and saved_d >= S_table[Ut]:
                z += 1
            if Ut >= 1 and saved_d >= S_table[Ut - 1]:
                z += 1
            if z > Ut - 1:
                z = Ut - 1
            
            # Extract sample value
            S = ((shifted >> 16) & 0xFFFF) >> (17 - Ut) & ((-1 << z) & 0xFFFF)
            S += saved_d << (Ut - 1)
            
            if (shifted & (1 << (32 - Ut + z))) != 0:
                S = ~(S | ((1 << z) - 1))
            
            u += d + Ut - z
            while u >= 8:
                n += 1
                u -= 8
            
            # Prediction
            pred_w = 0
            for dd in range(20):
                pred_w += self.qt[dd] * self.Kt[dd]
            
            if pred_w >= 0:
                pred_w = pred_w >> 12
            else:
                pred_w = (pred_w + 4095) >> 12
            
            Ot = self.Ot
            T_val = S * Ot + Ot // 2
            S_scaled = T_val >> 4
            
            for dd in range(19, 0, -1):
                self.qt[dd] += -(self.qt[dd] >> 7) + (self.Kt[dd] * S_scaled >> f_val)
                self.Kt[dd] = self.Kt[dd - 1]
            self.qt[0] += -(self.qt[0] >> 7) + (self.Kt[0] * S_scaled >> f_val)
            self.Kt[0] = pred_w + T_val
            
            sample = self.Kt[0] + (self.Qt >> 4)
            if (16 & jt):
                self.Qt = 0
            else:
                self.Qt = self.Qt + (self.Kt[0] << 4 >> 3)
            
            self.add_sample(sample)
            o_count += 1
        
        if u == 0:
            n -= 1
        
        return n
    
    def on_message(self, ws, message):
        """Handle incoming WebSocket binary message."""
        if isinstance(message, str):
            print(f"[TEXT] {message}")
            return
        
        data = message if isinstance(message, (bytes, bytearray)) else bytes(message)
        self.total_bytes_in += len(data)
        self.msg_count += 1
        
        n = 0
        while n < len(data):
            b = data[n]
            
            if (b & 0xF0) == 0xF0:
                # S-meter: 0xFx yy
                if n + 1 < len(data):
                    self.smeter = (b & 0x0F) * 256 + data[n + 1]
                    n += 1
                n += 1
                
            elif b == 0x80:
                # Mu-law audio block: 128 bytes follow
                if n + 128 < len(data):
                    self.decode_ulaw_block(data, n)
                    n += 129
                    # Reset prediction
                    self.qt = [0] * 20
                    self.Kt = [0] * 20
                    self.Qt = 0
                else:
                    break
                    
            elif 0x90 <= b <= 0xDF:
                # Compressed audio with shift parameter
                self.Ut = 14 - (b >> 4)
                u_init = 4
                n += 1
                n = self.decode_compressed_block(data, n, u_init, self.Ut)
                n += 1
                
            elif (b & 0x80) == 0:
                # Compressed audio, different start
                u_init = 1
                n += 1
                n = self.decode_compressed_block(data, n, u_init, self.Ut)
                n += 1
                
            elif b == 0x81:
                # Sample rate
                if n + 2 < len(data):
                    new_rate = data[n + 1] * 256 + data[n + 2]
                    if new_rate != self.sample_rate and new_rate > 0:
                        print(f"Sample rate: {new_rate} Hz")
                        self.sample_rate = new_rate
                        # Restart audio with new rate
                        if self.stream:
                            self.stream.stop_stream()
                            self.stream.close()
                        self.start_audio()
                    n += 3
                else:
                    break
                    
            elif b == 0x82:
                # Quantization parameter
                if n + 2 < len(data):
                    self.Ot = data[n + 1] * 256 + data[n + 2]
                    print(f"Quantization (Ot): {self.Ot}")
                    n += 3
                else:
                    break
                    
            elif b == 0x83:
                # Mode info
                if n + 1 < len(data):
                    self.jt = data[n + 1]
                    mode_names = {0: "SSB", 1: "AM", 4: "FM"}
                    filter_idx = self.jt & 0x0F
                    print(f"Mode info: 0x{self.jt:02X} (filter={filter_idx})")
                    n += 2
                else:
                    break
                    
            elif b == 0x84:
                # Silence block
                self.decode_silence_block()
                n += 1
                
            elif b == 0x85:
                # True frequency (6 bytes follow)
                if n + 6 < len(data):
                    w = (((data[n+1] & 0x0F) << 16) + (data[n+2] << 8) + data[n+3]) * 16777216 + \
                        (data[n+4] << 16) + (data[n+5] << 8) + data[n+6]
                    band_id = data[n+1] >> 4
                    self.true_freq = w
                    print(f"True freq: {w} Hz (band {band_id})")
                    n += 7
                else:
                    break
            else:
                # Unknown tag
                print(f"Unknown tag: 0x{b:02X} at offset {n}")
                n += 1
    
    def on_error(self, ws, error):
        print(f"WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print(f"WebSocket closed: {close_status_code} {close_msg}")
        self.running = False
    
    def on_open(self, ws):
        print("WebSocket connected!")
        self.running = True
    
    def tune(self, freq_khz, band=1, mode="am", lo=-4.0, hi=4.0):
        """Send tuning command."""
        # Mode encoding: 0=CW, 1=AM/LSB/USB (server handles), 4=FM
        mode_map = {"cw": 0, "lsb": 0, "usb": 0, "am": 1, "fm": 4}
        m = mode_map.get(mode.lower(), 1)
        
        param = f"f={freq_khz}&band={band}&lo={lo}&hi={hi}&mode={m}"
        cmd = f"GET /~~param?{param}"
        print(f"Tuning: {cmd}")
        if self.ws:
            self.ws.send(cmd)
    
    def connect_and_play(self, freq_khz=7106, band=1, mode="lsb"):
        """Connect to WebSDR and start playing audio."""
        url = f"ws://{self.host}:{self.port}/~~stream?v=11"
        print(f"Connecting to {url}")
        
        # Start with default sample rate
        self.start_audio()
        
        self.ws = websocket.WebSocketApp(
            url,
            on_open=lambda ws: self._on_open_and_tune(ws, freq_khz, band, mode),
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        
        # Start stats thread
        stats_thread = threading.Thread(target=self._stats_loop, daemon=True)
        stats_thread.start()
        
        # Run WebSocket (blocking)
        self.ws.run_forever()
    
    def _on_open_and_tune(self, ws, freq_khz, band, mode):
        self.on_open(ws)
        time.sleep(0.2)
        self.tune(freq_khz, band, mode)
    
    def _stats_loop(self):
        """Print stats every 5 seconds."""
        while True:
            time.sleep(5)
            if not self.running:
                break
            with self.buffer_lock:
                buf_samples = len(self.pcm_buffer) // 2
            print(f"[STATS] msgs={self.msg_count} bytes_in={self.total_bytes_in} "
                  f"samples_out={self.total_samples} buf={buf_samples} "
                  f"smeter={self.smeter} "
                  f"ulaw={self.ulaw_blocks} compressed={self.compressed_blocks} "
                  f"silence={self.silence_blocks}")
    
    def cleanup(self):
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.pa.terminate()


def main():
    # Default: tune to 40m band, 7106 kHz LSB (common amateur freq in Europe)
    freq = 7106
    mode = "lsb"
    band = 1  # band index (0-based from bandinfo array)
    
    if len(sys.argv) > 1:
        freq = float(sys.argv[1])
    if len(sys.argv) > 2:
        mode = sys.argv[2]
    if len(sys.argv) > 3:
        band = int(sys.argv[3])
    
    print(f"WebSDR Player - {HOST}:{PORT}")
    print(f"Frequency: {freq} kHz, Mode: {mode}, Band: {band}")
    print(f"Press Ctrl+C to stop\n")
    
    player = WebSDRPlayer(HOST, PORT)
    
    try:
        player.connect_and_play(freq, band, mode)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        player.cleanup()


if __name__ == "__main__":
    main()
