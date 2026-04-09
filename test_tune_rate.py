"""Test: how fast can we send tune commands before the server kicks us?"""
import websocket
import time
import threading
import sys

HOST = "sdr.websdrmaasbree.nl"
PORT = 8901

msg_count = 0
t0 = time.time()
tune_count = 0


def on_message(ws, msg):
    global msg_count
    msg_count += 1


def on_close(ws, code, msg):
    elapsed = time.time() - t0
    print(f"  [{elapsed:.1f}s] CLOSED after {msg_count} msgs, {tune_count} tunes. code={code}")


def on_error(ws, err):
    print(f"  [{time.time()-t0:.1f}s] ERROR: {err}")


def test_tune_rate(interval_sec, duration=30):
    """Send tune commands at a given interval and see how long the connection survives."""
    global msg_count, t0, tune_count

    print(f"\n=== Tune every {interval_sec}s for {duration}s ===")
    msg_count = 0
    tune_count = 0
    t0 = time.time()
    closed = threading.Event()

    def on_open(ws):
        print("  Connected!")
        time.sleep(0.3)
        ws.send("GET /~~param?f=6980&band=3&lo=-2.8&hi=-0.3&mode=0")

    def on_close_ev(ws, code, msg):
        on_close(ws, code, msg)
        closed.set()

    ws = websocket.WebSocketApp(
        f"ws://{HOST}:{PORT}/~~stream?v=11",
        on_open=on_open,
        on_message=on_message,
        on_close=on_close_ev,
        on_error=on_error,
    )

    ws_thread = threading.Thread(target=lambda: ws.run_forever(ping_interval=0), daemon=True)
    ws_thread.start()
    time.sleep(2)  # Let connection establish

    # Send tune commands at the specified rate
    freqs = [6980, 7106, 7200, 7000, 6900, 7050, 7150, 7100]
    idx = 0
    end_time = time.time() + duration

    while time.time() < end_time and not closed.is_set():
        freq = freqs[idx % len(freqs)]
        cmd = f"GET /~~param?f={freq}&band=3&lo=-2.8&hi=-0.3&mode=0"
        try:
            ws.send(cmd)
            tune_count += 1
            if tune_count % 5 == 0 or tune_count <= 3:
                print(f"  [{time.time()-t0:.1f}s] Sent tune #{tune_count} f={freq}")
        except Exception as e:
            print(f"  [{time.time()-t0:.1f}s] Send failed: {e}")
            break
        idx += 1
        closed.wait(timeout=interval_sec)

    elapsed = time.time() - t0
    if closed.is_set():
        print(f"  RESULT: DISCONNECTED after {elapsed:.1f}s, {tune_count} tunes")
        return False
    else:
        print(f"  RESULT: SURVIVED {duration}s with {tune_count} tunes")
        ws.close()
        return True


if __name__ == "__main__":
    # Test different tune rates
    rates = [2.0, 1.0, 0.5, 0.2]
    for rate in rates:
        ok = test_tune_rate(rate, duration=20)
        if not ok:
            print(f"\n*** Server kicks at tune interval={rate}s ***")
            break
        time.sleep(3)  # Gap between tests
