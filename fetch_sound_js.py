"""
Fetch websdr-sound.js (HTML5 audio implementation) from the WebSDR server.
"""
import socket

HOST = "websdr.ns0.it"
PORT = 8902

def fetch_url(path, max_bytes=500000):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((HOST, PORT))
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode())
    data = bytearray()
    while len(data) < max_bytes:
        try:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data.extend(chunk)
        except socket.timeout:
            break
    sock.close()
    return bytes(data)

for path in ["/websdr-sound.js", "/websdr-javasound.js", "/websdr5-html5.js"]:
    print(f"\n{'='*60}")
    print(f"Fetching {path}...")
    data = fetch_url(path)
    text = data.decode("utf-8", errors="replace")
    first_line = text.split("\n")[0]
    print(f"Response: {first_line[:100]}")
    
    if "404" in first_line:
        print("  >>> 404 Not Found")
        continue
    
    header_end = text.find("\r\n\r\n")
    if header_end >= 0:
        body = text[header_end+4:]
    else:
        body = text
    
    print(f"Body size: {len(body)} chars")
    
    fname = path.lstrip("/")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Saved to {fname}")
    
    # Show first 200 lines
    lines = body.split("\n")
    print(f"\nFirst 100 lines:")
    for i, line in enumerate(lines[:100]):
        print(f"  {i+1:4d}: {line}")
