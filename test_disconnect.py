"""Diagnostic test: find out why WebSocket disconnects."""
import websocket
import time
import sys

HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

ping_count = 0
pong_count = 0
msg_count = 0
t0 = time.time()


def on_message(ws, msg):
    global msg_count
    msg_count += 1
    if msg_count % 100 == 0:
        elapsed = time.time() - t0
        kind = "text" if isinstance(msg, str) else f"bin {len(msg)}B"
        print(f"  [{elapsed:.1f}s] msg #{msg_count} ({kind})")


def on_ping(ws, data):
    global ping_count
    ping_count += 1
    print(f"  [{time.time()-t0:.1f}s] GOT PING #{ping_count} (len={len(data)})")


def on_pong(ws, data):
    global pong_count
    pong_count += 1
    print(f"  [{time.time()-t0:.1f}s] GOT PONG #{pong_count}")


def on_open(ws):
    print("  Connected!")
    time.sleep(0.3)
    ws.send("GET /~~param?f=6980&band=1&lo=-4.0&hi=4.0&mode=1")
    print("  Tune sent")


def on_close(ws, code, msg):
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] CLOSED: code={code} msg={msg} msgs={msg_count} pings={ping_count}")


def on_error(ws, err):
    print(f"  [{time.time()-t0:.1f}s] ERROR: {err}")


def run_test(label, origin_header=None):
    global t0, msg_count, ping_count, pong_count
    print(f"=== {label} ===")

    header = {}
    if origin_header:
        header["Origin"] = origin_header

    ws = websocket.WebSocketApp(
        f"ws://{HOST}:{PORT}/~~stream?v=11",
        on_open=on_open,
        on_message=on_message,
        on_ping=on_ping,
        on_pong=on_pong,
        on_close=on_close,
        on_error=on_error,
        header=header,
    )

    t0 = time.time()
    msg_count = 0
    ping_count = 0
    pong_count = 0

    ws.run_forever(ping_interval=0)
    print(f"  Total time: {time.time()-t0:.1f}s")
    print()


if __name__ == "__main__":
    # Test 1: no Origin header
    run_test("Test WITHOUT Origin header")
    time.sleep(2)

    # Test 2: with Origin header
    run_test("Test WITH Origin header", origin_header=f"http://{HOST}:{PORT}")
