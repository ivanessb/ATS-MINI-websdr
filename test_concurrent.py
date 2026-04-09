"""Test: does the server kick one connection when two are open from the same IP?"""
import websocket
import time
import threading

HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

results = {}


def make_handlers(name):
    t0 = time.time()
    count = [0]

    def on_message(ws, msg):
        count[0] += 1

    def on_open(ws):
        print(f"  [{name}] Connected!")
        time.sleep(0.3)
        ws.send("GET /~~param?f=6980&band=3&lo=-2.8&hi=-0.3&mode=0")

    def on_close(ws, code, msg):
        elapsed = time.time() - t0
        print(f"  [{name}] CLOSED at {elapsed:.1f}s after {count[0]} msgs. code={code}")
        results[name] = ("closed", elapsed, count[0])

    def on_error(ws, err):
        print(f"  [{name}] ERROR: {err}")

    return on_open, on_message, on_close, on_error


def run_ws(name, delay=0):
    time.sleep(delay)
    on_open, on_message, on_close, on_error = make_handlers(name)
    ws = websocket.WebSocketApp(
        f"ws://{HOST}:{PORT}/~~stream?v=11",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close,
        on_error=on_error,
    )
    ws.run_forever(ping_interval=0)


if __name__ == "__main__":
    print("=== Test: TWO simultaneous connections ===")

    t1 = threading.Thread(target=run_ws, args=("conn1", 0), daemon=True)
    t2 = threading.Thread(target=run_ws, args=("conn2", 2), daemon=True)

    t1.start()
    t2.start()

    # Wait up to 30 seconds
    t1.join(timeout=35)
    t2.join(timeout=35)

    print(f"\nResults: {results}")
    if not results:
        print("Both still running after 35s — server allows concurrent connections")
    elif len(results) == 1:
        name = list(results.keys())[0]
        print(f"Only {name} was kicked — server may enforce single connection per IP")
    else:
        print("Both were kicked")
