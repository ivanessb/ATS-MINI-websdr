"""
Simple WebSDR WebSocket diagnostic - connect, receive, and dump protocol messages.
"""
import websocket
import struct
import time
import sys
import threading

HOST = "websdr.ns0.it"
PORT = 8902

msg_count = 0
total_bytes = 0
start_time = None

def on_message(ws, message):
    global msg_count, total_bytes, start_time
    if start_time is None:
        start_time = time.time()
    
    msg_count += 1
    
    if isinstance(message, str):
        print(f"[TEXT #{msg_count}] {message[:200]}")
        return
    
    data = message if isinstance(message, (bytes, bytearray)) else bytes(message)
    total_bytes += len(data)
    elapsed = time.time() - start_time
    
    # Dump first 20 messages in detail
    if msg_count <= 20:
        print(f"\n[MSG #{msg_count}] len={len(data)}  total={total_bytes}B  elapsed={elapsed:.1f}s")
        # Parse tags
        n = 0
        while n < len(data):
            b = data[n]
            tag_offset = n
            
            if (b & 0xF0) == 0xF0:
                if n + 1 < len(data):
                    smeter = (b & 0x0F) * 256 + data[n+1]
                    print(f"  @{tag_offset:4d}: SMETER = {smeter} (0x{b:02X} 0x{data[n+1]:02X})")
                    n += 2
                else:
                    n += 1
            elif b == 0x80:
                print(f"  @{tag_offset:4d}: ULAW_BLOCK (128 samples)")
                n += 129
            elif 0x90 <= b <= 0xDF:
                Ut = 14 - (b >> 4)
                remaining = len(data) - n - 1
                print(f"  @{tag_offset:4d}: COMPRESSED_A shift={Ut} (0x{b:02X}) remaining={remaining}B")
                # Skip remaining bytes (we'd need full decoding here)
                n = len(data)  # skip to end for now
            elif (b & 0x80) == 0:
                remaining = len(data) - n - 1
                print(f"  @{tag_offset:4d}: COMPRESSED_B (0x{b:02X}) remaining={remaining}B")
                n = len(data)  # skip to end
            elif b == 0x81:
                if n + 2 < len(data):
                    rate = data[n+1] * 256 + data[n+2]
                    print(f"  @{tag_offset:4d}: SAMPLE_RATE = {rate} Hz")
                    n += 3
                else:
                    n += 1
            elif b == 0x82:
                if n + 2 < len(data):
                    ot = data[n+1] * 256 + data[n+2]
                    print(f"  @{tag_offset:4d}: QUANT_PARAM = {ot}")
                    n += 3
                else:
                    n += 1
            elif b == 0x83:
                if n + 1 < len(data):
                    jt = data[n+1]
                    print(f"  @{tag_offset:4d}: MODE_INFO = 0x{jt:02X} (filter={jt&0x0F})")
                    n += 2
                else:
                    n += 1
            elif b == 0x84:
                print(f"  @{tag_offset:4d}: SILENCE_BLOCK (128 zeros)")
                n += 1
            elif b == 0x85:
                if n + 6 < len(data):
                    print(f"  @{tag_offset:4d}: TRUE_FREQ bytes={data[n+1:n+7].hex()}")
                    n += 7
                else:
                    n += 1
            else:
                print(f"  @{tag_offset:4d}: UNKNOWN tag=0x{b:02X}")
                n += 1
    elif msg_count % 50 == 0:
        rate = total_bytes / elapsed if elapsed > 0 else 0
        print(f"[MSG #{msg_count}] len={len(data)} total={total_bytes}B "
              f"elapsed={elapsed:.1f}s rate={rate:.0f} B/s")

def on_error(ws, error):
    print(f"ERROR: {error}")

def on_close(ws, code, msg):
    print(f"CLOSED: code={code} msg={msg}")

def on_open(ws):
    print("CONNECTED!")
    # Send tuning command after brief delay
    def tune():
        time.sleep(0.5)
        freq = 7106
        band = 1
        mode = 0  # LSB
        cmd = f"GET /~~param?f={freq}&band={band}&lo=-4.0&hi=4.0&mode={mode}"
        print(f"Sending: {cmd}")
        ws.send(cmd)
    threading.Thread(target=tune, daemon=True).start()

url = f"ws://{HOST}:{PORT}/~~stream?v=11"
print(f"Connecting to {url}")

ws = websocket.WebSocketApp(
    url,
    on_open=on_open,
    on_message=on_message, 
    on_error=on_error,
    on_close=on_close,
)

try:
    ws.run_forever(ping_interval=30, ping_timeout=10)
except KeyboardInterrupt:
    print("\nDone")
