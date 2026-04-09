"""
Capture and trace decode of the first few WebSocket messages from the WebSDR.
This helps debug the compressed audio decoder by showing exact bit-level operations.
"""
import websocket
import struct
import time
import threading
import sys
import json

HOST = "websdr.ns0.it"
PORT = 8902
S_TABLE = [999, 999, 8, 4, 2, 1, 99, 99]

def u32(x): return x & 0xFFFFFFFF
def s32(x):
    x = x & 0xFFFFFFFF
    return x if x < 0x80000000 else x - 0x100000000
def asr32(v, shift):
    v = s32(v)
    return v >> shift

class Tracer:
    def __init__(self):
        self.messages = []
        self.Ut = 0
        self.Ot = 40
        self.jt = 0
        self.qt = [0]*20
        self.Kt = [0]*20
        self.Qt = 0
        self.done = False
        
    def decode_compressed_trace(self, t, n_start, u_start, msg_idx, trace_samples=5):
        """Decode with detailed trace of first few samples."""
        n = n_start
        u = u_start
        Ut = self.Ut
        Ot = self.Ot
        jt = self.jt
        f = 12 if (jt & 16) else 14
        qt = self.qt
        Kt = self.Kt
        Qt = self.Qt
        
        samples = []
        for si in range(128):
            if n + 3 >= len(t):
                print(f"  !!! Buffer overflow at sample {si}, n={n}, len={len(t)}")
                break
                
            w_orig = u32((t[n]<<24)|(t[n+1]<<16)|(t[n+2]<<8)|t[n+3])
            n_before = n
            u_before = u
            
            d = 0
            underscore = 15 - Ut
            T = Ot
            w = u32(w_orig << u)
            
            if w != 0:
                while (w & 0x80000000) == 0 and d < underscore:
                    w = u32(w << 1)
                    d += 1
                if d < underscore:
                    underscore = d
                    d += 1
                    w = u32(w << 1)
                else:
                    underscore = (w >> 24) & 0xFF
                    d += 8
                    w = u32(w << 8)
            else:
                underscore = 0
                d += 8
                w = u32(w << 8)
            
            z = 0
            if underscore >= S_TABLE[Ut]: z += 1
            if underscore >= S_TABLE[Ut-1]: z += 1
            if z > Ut - 1: z = Ut - 1
            
            S_val = (((w>>16)&0xFFFF) >> (17-Ut)) & (s32(-1<<z) & 0xFFFF)
            S_val += underscore << (Ut-1)
            
            sign_bit = 32 - Ut + z
            if w & u32(1 << sign_bit):
                S_val = s32(~(S_val | ((1<<z)-1)))
            
            bits_consumed = d + Ut - z
            u += bits_consumed
            while u >= 8:
                n += 1
                u -= 8
            
            pred_sum = 0
            for i in range(20):
                pred_sum += qt[i] * Kt[i]
            pred_sum = s32(pred_sum)
            pred_out = pred_sum >> 12 if pred_sum >= 0 else (pred_sum + 4095) >> 12
            
            T_val = s32(S_val * T + (T >> 1))
            S_scaled = asr32(T_val, 4)
            
            for i in range(19, 0, -1):
                decay = -(asr32(qt[i], 7))
                adapt = asr32(s32(Kt[i] * S_scaled), f)
                qt[i] = s32(qt[i] + decay + adapt)
                Kt[i] = Kt[i-1]
            decay0 = -(asr32(qt[0], 7))
            adapt0 = asr32(s32(Kt[0] * S_scaled), f)
            qt[0] = s32(qt[0] + decay0 + adapt0)
            
            Kt[0] = s32(pred_out + T_val)
            sample = s32(Kt[0] + asr32(Qt, 4))
            if jt & 16:
                Qt = 0
            else:
                Qt = s32(Qt + asr32(s32(Kt[0]<<4), 3))
            
            samples.append(sample)
            
            if si < trace_samples:
                print(f"  sample[{si}]: n={n_before}+{n-n_before} u={u_before}->{u} "
                      f"w=0x{w_orig:08X} d={d-Ut+z+bits_consumed-bits_consumed}.. "
                      f"lz={underscore} z={z} bits={bits_consumed} "
                      f"S={S_val} T={T_val} pred={pred_out} -> {sample}")
        
        self.qt = qt
        self.Kt = Kt
        self.Qt = Qt
        
        if u == 0:
            n -= 1
            
        bytes_consumed = n - n_start + (1 if u > 0 else 1)
        print(f"  Decoded {len(samples)} samples, consumed {bytes_consumed} bytes "
              f"(start={n_start} end_n={n} end_u={u})")
        return n, samples
    
    def on_message(self, ws, message):
        if isinstance(message, str):
            return
            
        t = message if isinstance(message, (bytes, bytearray)) else bytes(message)
        msg_idx = len(self.messages)
        self.messages.append(bytes(t))
        
        if msg_idx >= 15:  # capture first 15 messages
            self.done = True
            ws.close()
            return
        
        print(f"\n{'='*60}")
        print(f"MSG #{msg_idx}: {len(t)} bytes")
        print(f"  Hex: {t[:32].hex()}")
        
        n = 0
        while n < len(t):
            b = t[n]
            
            if (b & 0xF0) == 0xF0:
                if n+1 < len(t):
                    sm = (b&0x0F)*256 + t[n+1]
                    print(f"  @{n}: SMETER={sm}")
                    n += 2
                else: n += 1
                    
            elif b == 0x80:
                print(f"  @{n}: ULAW block (128 bytes)")
                n += 129
                self.qt = [0]*20
                self.Kt = [0]*20
                self.Qt = 0
                
            elif 0x90 <= b <= 0xDF:
                self.Ut = 14 - (b >> 4)
                print(f"  @{n}: COMP_A tag=0x{b:02X} Ut={self.Ut}")
                # n stays at tag byte, decoder reads from here with u=4
                n, samps = self.decode_compressed_trace(t, n, 4, msg_idx)
                n += 1
                
            elif (b & 0x80) == 0:
                print(f"  @{n}: COMP_B firstbyte=0x{b:02X} Ut={self.Ut}")
                n, samps = self.decode_compressed_trace(t, n, 1, msg_idx)
                n += 1
                
            elif b == 0x81:
                if n+2 < len(t):
                    rate = t[n+1]*256 + t[n+2]
                    print(f"  @{n}: SAMPLE_RATE={rate}")
                    n += 3
                else: break
                    
            elif b == 0x82:
                if n+2 < len(t):
                    self.Ot = t[n+1]*256 + t[n+2]
                    print(f"  @{n}: QUANT={self.Ot}")
                    n += 3
                else: break
                    
            elif b == 0x83:
                if n+1 < len(t):
                    self.jt = t[n+1]
                    print(f"  @{n}: MODE=0x{self.jt:02X}")
                    n += 2
                else: break
                    
            elif b == 0x84:
                print(f"  @{n}: SILENCE")
                self.qt = [0]*20; self.Kt = [0]*20; self.Qt = 0
                n += 1
                
            elif b == 0x85:
                if n+6 < len(t):
                    print(f"  @{n}: TRUEFREQ bytes={t[n+1:n+7].hex()}")
                    n += 7
                else: break
                    
            else:
                print(f"  @{n}: UNKNOWN 0x{b:02X}")
                n += 1
    
    def on_open(self, ws):
        print("Connected!")
        def tune():
            time.sleep(0.5)
            ws.send("GET /~~param?f=7106&band=1&lo=-2.8&hi=-0.3&mode=0")
            print("Tuned to 7106 kHz LSB")
        threading.Thread(target=tune, daemon=True).start()
    
    def on_error(self, ws, e):
        print(f"Error: {e}")
    
    def on_close(self, ws, code, msg):
        print(f"Closed: {code}")

tracer = Tracer()
url = f"ws://{HOST}:{PORT}/~~stream?v=11"
print(f"Connecting to {url}...")
ws = websocket.WebSocketApp(url,
    on_open=tracer.on_open,
    on_message=tracer.on_message,
    on_error=tracer.on_error,
    on_close=tracer.on_close)
try:
    ws.run_forever(ping_interval=0)
except KeyboardInterrupt:
    pass
print("\nDone")
