"""
Fetch WebSDR JavaScript source to reverse-engineer the streaming protocol.
"""
import socket
import sys

HOST = "websdr.ns0.it"
PORT = 8902

# Try common WebSDR JS filenames
js_files = [
    "/websdr5-html5.js",
    "/websdr-sound.js", 
    "/websdr.js",
    "/sound.js",
    "/js/websdr.js",
]

def fetch_url(path, max_bytes=200000):
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

def fetch_main_page():
    """Get the main HTML page to find JS script references."""
    print("Fetching main page...")
    data = fetch_url("/")
    text = data.decode("utf-8", errors="replace")
    
    # Find \r\n\r\n to skip headers
    header_end = text.find("\r\n\r\n")
    if header_end >= 0:
        body = text[header_end+4:]
    else:
        body = text
    
    # Find all .js references
    import re
    scripts = re.findall(r'(?:src|SRC)\s*=\s*["\']([^"\']*\.js[^"\']*)["\']', body)
    print(f"Found {len(scripts)} script references:")
    for s in scripts:
        print(f"  {s}")
    
    # Also look for inline script with stream/sound/audio keywords
    inline_matches = re.findall(r'(~~stream|sound|audio|setfreq|freq=|band=|samplerate)', body, re.IGNORECASE)
    if inline_matches:
        print(f"\nRelevant inline keywords: {set(inline_matches)}")
    
    return scripts, body

def fetch_and_analyze_js(path):
    """Fetch a JS file and search for stream/audio protocol code."""
    print(f"\n{'='*60}")
    print(f"Fetching {path}...")
    data = fetch_url(path)
    
    # Check for HTTP error
    text = data.decode("utf-8", errors="replace")
    first_line = text.split("\n")[0] if text else ""
    print(f"Response: {first_line[:80]}")
    
    if "404" in first_line or "Not Found" in first_line:
        print("  >>> 404 Not Found")
        return None
    
    # Skip headers
    header_end = text.find("\r\n\r\n")
    if header_end >= 0:
        body = text[header_end+4:]
    else:
        body = text
    
    print(f"Body size: {len(body)} chars")
    
    # Search for relevant protocol code
    import re
    keywords = [
        r'~~stream',
        r'setfreq',
        r'freq=',
        r'band=',
        r'samplerate',
        r'sample_rate',
        r'audioContext',
        r'decodeAudio',
        r'Int8Array|Int16Array|Float32Array',
        r'createBuffer|createScriptProcessor|audioWorklet',
        r'snd_',
        r'soundapplet',
        r'xhr|XMLHttpRequest|fetch',
        r'WebSocket|websocket',
        r'\.send\(',
        r'arraybuffer',
        r'binary',
        r'getAudio|playAudio',
        r'DataView|ArrayBuffer',
        r'PCM|pcm|adpcm|ADPCM|ulaw|alaw',
    ]
    
    for kw in keywords:
        matches = [(m.start(), m.group()) for m in re.finditer(kw, body, re.IGNORECASE)]
        if matches:
            print(f"\n  Keyword '{kw}': {len(matches)} occurrences")
            for pos, match in matches[:3]:
                # Show context (60 chars before and after)
                start = max(0, pos - 80)
                end = min(len(body), pos + 80)
                context = body[start:end].replace("\n", " ").strip()
                print(f"    ...{context}...")
    
    return body

def save_js(name, content):
    fname = name.replace("/", "_").lstrip("_")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  Saved to {fname}")

def main():
    # Step 1: Get main page and find JS files
    scripts, html = fetch_main_page()
    
    # Step 2: Look in the HTML for ~~stream references with more context
    import re
    for m in re.finditer(r'.{0,200}~~stream.{0,200}', html, re.DOTALL):
        print(f"\n~~stream context in HTML:\n  {m.group()[:400]}")
    
    # Step 3: Fetch each JS file
    all_js = []
    for script_path in scripts:
        if script_path.startswith("http"):
            continue  # Skip external URLs
        if not script_path.startswith("/"):
            script_path = "/" + script_path
        body = fetch_and_analyze_js(script_path)
        if body:
            all_js.append((script_path, body))
            save_js(script_path, body)
    
    # Also try known WebSDR JS filenames not found in page
    for path in js_files:
        already_fetched = any(s == path for s, _ in all_js)
        if not already_fetched:
            body = fetch_and_analyze_js(path)
            if body and "404" not in body[:100]:
                all_js.append((path, body))
                save_js(path, body)
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: Fetched {len(all_js)} JS files")
    print("Now search these files for the audio streaming protocol.")

if __name__ == "__main__":
    main()
