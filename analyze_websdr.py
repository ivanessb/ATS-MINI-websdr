"""
WebSDR Stream Analyzer
Connects to websdr.ns0.it:8902, captures the raw binary stream,
and analyzes the protocol format to determine the audio codec.
"""

import socket
import struct
import time
import sys
import os
from collections import Counter

HOST = "websdr.ns0.it"
PORT = 8902
STREAM_PATH = "/~~stream"
FREQ = 14100
BAND = 5
MODE = "am"
CAPTURE_SECONDS = 10
DUMP_FILE = "websdr_capture.bin"
WAV_FILE = "websdr_output.wav"


def connect_and_capture():
    """Connect to WebSDR and capture raw bytes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(15)

    print(f"Connecting to {HOST}:{PORT}...")
    sock.connect((HOST, PORT))
    print("TCP connected.")

    # Send the same HTTP GET we use on the ESP32
    request = (
        f"GET {STREAM_PATH}?freq={FREQ}&band={BAND}&lo=-4.0&hi=4.0&mode={MODE} HTTP/1.1\r\n"
        f"Host: {HOST}:{PORT}\r\n"
        f"Accept: */*\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    print(f"Sending request:\n{request}")
    sock.sendall(request.encode())

    # Capture data for N seconds
    print(f"Capturing {CAPTURE_SECONDS} seconds of data...")
    data = bytearray()
    start = time.time()
    while time.time() - start < CAPTURE_SECONDS:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                print("Server closed connection.")
                break
            data.extend(chunk)
        except socket.timeout:
            print("Receive timeout.")
            break

    elapsed = time.time() - start
    sock.close()

    print(f"\nCaptured {len(data)} bytes in {elapsed:.1f}s ({len(data)/elapsed:.0f} bytes/sec)")
    return bytes(data)


def analyze_header(data):
    """Analyze the first bytes to detect protocol framing."""
    print("\n" + "=" * 60)
    print("HEADER ANALYSIS (first 64 bytes)")
    print("=" * 60)

    # Hex dump
    for i in range(0, min(64, len(data)), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in data[i:i+16])
        print(f"  {i:04X}: {hex_part:<48s}  {ascii_part}")

    # Check for HTTP response
    if data[:4] == b"HTTP":
        print("\n>>> Server sent HTTP response headers!")
        header_end = data.find(b"\r\n\r\n")
        if header_end >= 0:
            print(data[:header_end].decode("utf-8", errors="replace"))
            return header_end + 4
        return 0

    # Check for gzip (0x1F 0x8B)
    for i in range(min(8, len(data))):
        if i + 1 < len(data) and data[i] == 0x1F and data[i + 1] == 0x8B:
            print(f"\n>>> Gzip magic found at offset {i}!")

    # Check for common audio markers
    if data[:4] == b"OggS":
        print("\n>>> Ogg container detected!")
    if data[:4] == b"fLaC":
        print("\n>>> FLAC audio detected!")
    if data[:4] == b"RIFF":
        print("\n>>> WAV/RIFF format detected!")

    # Check if first 2 bytes look like a length prefix
    if len(data) >= 2:
        val16 = struct.unpack("<H", data[:2])[0]
        val16be = struct.unpack(">H", data[:2])[0]
        print(f"\n  First 2 bytes as uint16 LE: {val16}")
        print(f"  First 2 bytes as uint16 BE: {val16be}")

    if len(data) >= 4:
        val32 = struct.unpack("<I", data[:4])[0]
        val32be = struct.unpack(">I", data[:4])[0]
        print(f"  First 4 bytes as uint32 LE: {val32}")
        print(f"  First 4 bytes as uint32 BE: {val32be}")

    return 0


def find_repeating_patterns(data):
    """Look for repeating frame headers or block boundaries."""
    print("\n" + "=" * 60)
    print("FRAME/BLOCK PATTERN ANALYSIS")
    print("=" * 60)

    # Look for recurring byte patterns that might be frame headers
    # Check if 0x01 appears periodically (it was the first byte)
    first_byte = data[0]
    positions = [i for i in range(len(data)) if data[i] == first_byte and i < 10000]
    if len(positions) > 2:
        diffs = [positions[i+1] - positions[i] for i in range(min(30, len(positions)-1))]
        if diffs:
            print(f"\n  Byte 0x{first_byte:02X} appears at offsets (first 20): {positions[:20]}")
            print(f"  Spacing between occurrences: {diffs[:20]}")
            counter = Counter(diffs)
            print(f"  Most common spacings: {counter.most_common(5)}")

    # Look for 2-byte header patterns that repeat
    print("\n  Scanning for 2-byte headers that repeat at regular intervals...")
    header = data[:2]
    header_positions = []
    for i in range(0, min(len(data), 20000)):
        if data[i:i+2] == header:
            header_positions.append(i)

    if len(header_positions) > 2:
        diffs = [header_positions[i+1] - header_positions[i] for i in range(len(header_positions)-1)]
        print(f"  Header {header.hex()} at positions (first 20): {header_positions[:20]}")
        print(f"  Spacings: {diffs[:20]}")
        counter = Counter(diffs)
        print(f"  Most common spacings: {counter.most_common(5)}")


def analyze_byte_distribution(data, offset=0, label="full stream"):
    """Analyze byte value distribution to detect encoding."""
    print(f"\n  Byte distribution for {label} ({len(data)-offset} bytes from offset {offset}):")
    d = data[offset:]
    if len(d) == 0:
        return

    values = list(d)
    avg = sum(values) / len(values)
    mn, mx = min(values), max(values)
    median = sorted(values)[len(values) // 2]

    # Histogram buckets
    hist = [0] * 16
    for v in values:
        hist[v // 16] += 1

    print(f"    Min={mn}  Max={mx}  Mean={avg:.1f}  Median={median}")
    print(f"    Histogram (16 buckets of 16 values each):")
    max_count = max(hist) if max(hist) > 0 else 1
    for i, count in enumerate(hist):
        bar = "#" * int(40 * count / max_count)
        print(f"      {i*16:3d}-{i*16+15:3d}: {count:6d} {bar}")

    # Check if centered around 128 (unsigned 8-bit PCM silence)
    if 120 < avg < 136:
        print("    >>> Mean near 128 — consistent with unsigned 8-bit PCM")
    elif -5 < (avg - 0) < 5:
        print("    >>> Mean near 0 — consistent with signed 8-bit PCM")

    # Check entropy (high entropy = compressed or encrypted)
    unique = len(set(values))
    print(f"    Unique byte values: {unique}/256")
    if unique > 250:
        print("    >>> Very high entropy — likely compressed data or noise")
    elif unique < 50:
        print("    >>> Low entropy — likely simple PCM or sparse data")


def try_gzip_decompress(data, offset=0):
    """Try gzip decompression at various offsets."""
    import gzip
    import io

    print("\n" + "=" * 60)
    print("GZIP DECOMPRESSION ATTEMPTS")
    print("=" * 60)

    for off in range(offset, min(offset + 16, len(data))):
        try:
            stream = io.BytesIO(data[off:off + min(4096, len(data) - off)])
            with gzip.GzipFile(fileobj=stream) as f:
                decompressed = f.read()
            print(f"  Offset {off}: GZIP SUCCESS! Decompressed {len(decompressed)} bytes")
            print(f"    First 32 bytes: {decompressed[:32].hex()}")
            return off, decompressed
        except Exception as e:
            pass  # Not gzip at this offset

    print("  No valid gzip data found in header region.")
    return None, None


def try_zlib_decompress(data, offset=0):
    """Try zlib/deflate decompression at various offsets."""
    import zlib

    print("\n" + "=" * 60)
    print("ZLIB/DEFLATE DECOMPRESSION ATTEMPTS")
    print("=" * 60)

    for off in range(offset, min(offset + 16, len(data))):
        for wbits in [15, -15, 31, 47]:  # zlib, raw deflate, gzip, auto
            try:
                decompressed = zlib.decompress(data[off:off + min(8192, len(data) - off)], wbits)
                label = {15: "zlib", -15: "raw deflate", 31: "gzip", 47: "auto"}[wbits]
                print(f"  Offset {off}, mode={label}: SUCCESS! Decompressed {len(decompressed)} bytes")
                print(f"    First 32 bytes: {decompressed[:32].hex()}")
                analyze_byte_distribution(decompressed, 0, f"decompressed ({label} at offset {off})")
                return off, decompressed
            except Exception:
                pass

    print("  No valid zlib/deflate data found in header region.")
    return None, None


def scan_for_framing(data):
    """Look for length-prefixed framing in the stream."""
    print("\n" + "=" * 60)
    print("LENGTH-PREFIX FRAMING SCAN")
    print("=" * 60)

    # Try: 1-byte type + 2-byte length (LE)
    print("\n  Trying: [1-byte type] [2-byte LE length] [payload]...")
    offset = 0
    frames = []
    for attempt in range(20):
        if offset + 3 > len(data):
            break
        frame_type = data[offset]
        frame_len = struct.unpack("<H", data[offset+1:offset+3])[0]
        if frame_len == 0 or frame_len > 10000:
            break
        frames.append((offset, frame_type, frame_len))
        offset += 3 + frame_len

    if len(frames) > 3:
        print(f"  Found {len(frames)} consistent frames!")
        for off, ft, fl in frames[:10]:
            preview = data[off+3:off+3+min(8, fl)].hex() if fl > 0 else ""
            print(f"    offset={off:5d}  type=0x{ft:02X}  len={fl:5d}  data={preview}...")
        return frames

    # Try: 2-byte length (BE) + payload
    print("  Trying: [2-byte BE length] [payload]...")
    offset = 0
    frames = []
    for attempt in range(20):
        if offset + 2 > len(data):
            break
        frame_len = struct.unpack(">H", data[offset:offset+2])[0]
        if frame_len == 0 or frame_len > 10000:
            break
        frames.append((offset, frame_len))
        offset += 2 + frame_len

    if len(frames) > 3:
        print(f"  Found {len(frames)} consistent frames!")
        for off, fl in frames[:10]:
            preview = data[off+2:off+2+min(8, fl)].hex() if fl > 0 else ""
            print(f"    offset={off:5d}  len={fl:5d}  data={preview}...")
        return frames

    # Try: 1-byte type + 1-byte length
    print("  Trying: [1-byte type] [1-byte length] [payload]...")
    offset = 0
    frames = []
    for attempt in range(30):
        if offset + 2 > len(data):
            break
        frame_type = data[offset]
        frame_len = data[offset + 1]
        if frame_len == 0:
            break
        frames.append((offset, frame_type, frame_len))
        offset += 2 + frame_len

    if len(frames) > 5:
        print(f"  Found {len(frames)} consistent frames!")
        for off, ft, fl in frames[:10]:
            preview = data[off+2:off+2+min(8, fl)].hex() if fl > 0 else ""
            print(f"    offset={off:5d}  type=0x{ft:02X}  len={fl:3d}  data={preview}...")
        return frames

    print("  No consistent length-prefix framing found.")
    return None


def try_play_raw_pcm(data, sample_rate=8000, channels=1, offset=0):
    """Save as WAV for playback testing."""
    import wave

    d = data[offset:]
    print(f"\n  Saving {len(d)} bytes as WAV (unsigned 8-bit, {sample_rate}Hz, {channels}ch)...")

    with wave.open(WAV_FILE, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(1)  # 8-bit
        wf.setframerate(sample_rate)
        wf.writeframes(d)

    duration = len(d) / sample_rate / channels
    print(f"  Saved to {WAV_FILE} ({duration:.1f}s)")
    print(f"  Play with: start {WAV_FILE}")


def main():
    print("=" * 60)
    print("WebSDR Stream Analyzer")
    print(f"Target: {HOST}:{PORT}{STREAM_PATH}")
    print("=" * 60)

    # Capture
    data = connect_and_capture()
    if len(data) < 100:
        print("ERROR: Not enough data captured.")
        return

    # Save raw capture
    with open(DUMP_FILE, "wb") as f:
        f.write(data)
    print(f"Raw capture saved to {DUMP_FILE}")

    # Analyze header
    body_offset = analyze_header(data)

    # Byte distribution
    print("\n" + "=" * 60)
    print("BYTE VALUE DISTRIBUTION")
    print("=" * 60)
    analyze_byte_distribution(data, body_offset, "raw stream")

    # Look for framing
    frames = scan_for_framing(data[body_offset:])

    # Pattern analysis
    find_repeating_patterns(data[body_offset:])

    # Try decompression
    gzip_off, gzip_data = try_gzip_decompress(data, body_offset)
    zlib_off, zlib_data = try_zlib_decompress(data, body_offset)

    # If decompressed data found, analyze it
    decompressed = gzip_data or zlib_data
    if decompressed:
        print("\n" + "=" * 60)
        print("DECOMPRESSED DATA ANALYSIS")
        print("=" * 60)
        print(f"  First 64 bytes hex:")
        for i in range(0, min(64, len(decompressed)), 16):
            print(f"    {i:04X}: {decompressed[i:i+16].hex(' ')}")
        analyze_byte_distribution(decompressed, 0, "decompressed data")
        try_play_raw_pcm(decompressed, 8000)
    else:
        # No decompression worked — try raw
        print("\n" + "=" * 60)
        print("RAW PCM INTERPRETATION")
        print("=" * 60)

        # Try different offsets (skip potential header bytes)
        for skip in [0, 1, 2, 4]:
            subset = data[body_offset + skip: body_offset + skip + 2000]
            if len(subset) > 100:
                analyze_byte_distribution(subset, 0, f"first 2000 bytes (skip {skip})")

        try_play_raw_pcm(data, 8000, 1, body_offset)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    data_rate = len(data) / CAPTURE_SECONDS
    print(f"  Data rate: {data_rate:.0f} bytes/sec")
    print(f"  If 8-bit PCM @ 8kHz: expected 8000 bytes/sec, got {data_rate:.0f}")
    print(f"  If 16-bit PCM @ 8kHz: expected 16000 bytes/sec, got {data_rate:.0f}")
    print(f"  If 8-bit PCM @ 11.025kHz: expected 11025 bytes/sec, got {data_rate:.0f}")
    print(f"  If ADPCM 4-bit @ 8kHz: expected 4000 bytes/sec, got {data_rate:.0f}")
    print(f"  Data rate matches: ", end="")
    if 3500 < data_rate < 4500:
        print("4-bit ADPCM @ 8kHz")
    elif 7000 < data_rate < 9000:
        print("8-bit PCM @ 8kHz or ADPCM @ 16kHz")
    elif 10000 < data_rate < 12000:
        print("8-bit PCM @ 11.025kHz or compressed @ higher rate")
    elif 15000 < data_rate < 17000:
        print("16-bit PCM @ 8kHz")
    else:
        print(f"unknown format ({data_rate:.0f} B/s)")


if __name__ == "__main__":
    main()
