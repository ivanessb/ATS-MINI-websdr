"""
WebSDR Stream Decoder — Try multiple audio interpretations.
Reads the captured binary and saves WAV files for each decoding attempt.
"""

import struct
import wave
import os
import audioop
import sys

DUMP_FILE = "websdr_capture.bin"


def ulaw_decode_table():
    """Build u-law to 16-bit PCM decode table."""
    table = []
    for i in range(256):
        # Complement
        val = ~i & 0xFF
        sign = val & 0x80
        exponent = (val >> 4) & 0x07
        mantissa = val & 0x0F
        sample = (mantissa << 3) + 0x84
        sample <<= exponent
        sample -= 0x84
        if sign:
            sample = -sample
        table.append(sample)
    return table


def alaw_decode_table():
    """Build A-law to 16-bit PCM decode table."""
    table = []
    for i in range(256):
        val = i ^ 0x55
        sign = val & 0x80
        exponent = (val >> 4) & 0x07
        mantissa = val & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        if sign:
            sample = -sample
        table.append(sample)
    return table


def save_wav_16bit(filename, samples_16bit, sample_rate=8000, channels=1):
    """Save signed 16-bit samples as WAV."""
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw = struct.pack(f"<{len(samples_16bit)}h", *samples_16bit)
        wf.writeframes(raw)
    dur = len(samples_16bit) / sample_rate / channels
    print(f"  Saved {filename} ({dur:.1f}s, {len(samples_16bit)} samples)")


def save_wav_8bit(filename, data, sample_rate=8000, channels=1):
    """Save unsigned 8-bit data as WAV."""
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(1)
        wf.setframerate(sample_rate)
        wf.writeframes(data)
    dur = len(data) / sample_rate / channels
    print(f"  Saved {filename} ({dur:.1f}s, {len(data)} bytes)")


def parse_header(data):
    """Try to parse the initial protocol header."""
    print("=" * 60)
    print("PROTOCOL HEADER PARSING")
    print("=" * 60)

    print(f"\nFirst 32 bytes hex:")
    for i in range(0, min(32, len(data)), 16):
        hex_part = " ".join(f"{b:02X}" for b in data[i:i+16])
        print(f"  {i:04X}: {hex_part}")

    # Interpretation: tagged parameters
    # 0x81 = tag1, 0x82 = tag2, 0x83 = tag3 (high bit = tag marker)
    print(f"\nTagged parameter interpretation:")
    print(f"  Byte 0: 0x{data[0]:02X} (message type = {data[0]})")
    print(f"  Byte 1: 0x{data[1]:02X} (subtype/version = {data[1]})")

    header_len = 2  # minimum
    i = 2
    params = {}
    while i < min(20, len(data)):
        if data[i] & 0x80:
            tag = data[i] & 0x7F
            if i + 2 < len(data):
                val16 = struct.unpack(">H", data[i+1:i+3])[0]
                print(f"  Tag 0x{data[i]:02X} (param {tag}): 0x{data[i+1]:02X}{data[i+2]:02X} = {val16}")
                params[tag] = val16
                header_len = i + 3
                i += 3
            elif i + 1 < len(data):
                val8 = data[i+1]
                print(f"  Tag 0x{data[i]:02X} (param {tag}): 0x{val8:02X} = {val8}")
                params[tag] = val8
                header_len = i + 2
                i += 2
            else:
                break
        else:
            # Not a tag — end of header?
            break

    # If tag parsing stops working, try fixed offsets
    if 1 in params:
        print(f"\n  >>> Param 1 = {params[1]} (likely sample rate: {params[1]} Hz)")
    if 2 in params:
        print(f"  >>> Param 2 = {params[2]} (bandwidth? filter? {params[2]})")
    if 3 in params and params[3] <= 32:
        print(f"  >>> Param 3 = {params[3]} (likely bits per sample: {params[3]})")

    # Auto-detect header size by scanning for where tags stop
    print(f"\n  Detected header length: {header_len} bytes")
    print(f"  Audio data starts at offset {header_len}")

    return header_len, params


def try_all_decodings(data, header_len):
    """Try multiple audio decodings and save WAV files."""
    audio_data = data[header_len:]
    print(f"\n  Audio payload: {len(audio_data)} bytes")

    results = []

    for skip in [0, header_len]:
        d = data[skip:]
        label = f"skip{skip}"

        # 1. Raw unsigned 8-bit PCM at 8kHz
        fname = f"decode_{label}_u8_8k.wav"
        save_wav_8bit(fname, d, 8000)
        results.append(fname)

        # 2. Raw unsigned 8-bit PCM at 6000Hz (closer to actual data rate)
        fname = f"decode_{label}_u8_6k.wav"
        save_wav_8bit(fname, d, 6000)
        results.append(fname)

        # 3. u-law decode
        ulaw_table = ulaw_decode_table()
        samples = [ulaw_table[b] for b in d]
        fname = f"decode_{label}_ulaw_8k.wav"
        save_wav_16bit(fname, samples, 8000)
        results.append(fname)

        fname = f"decode_{label}_ulaw_6k.wav"
        save_wav_16bit(fname, samples, 6000)
        results.append(fname)

        # 4. A-law decode
        alaw_table = alaw_decode_table()
        samples = [alaw_table[b] for b in d]
        fname = f"decode_{label}_alaw_8k.wav"
        save_wav_16bit(fname, samples, 8000)
        results.append(fname)

        # 5. Signed 16-bit LE PCM at 8kHz
        if len(d) >= 2:
            n_samples = len(d) // 2
            samples = list(struct.unpack(f"<{n_samples}h", d[:n_samples*2]))
            fname = f"decode_{label}_s16le_8k.wav"
            save_wav_16bit(fname, samples, 8000)
            results.append(fname)

        # 6. Signed 16-bit BE PCM at 8kHz
        if len(d) >= 2:
            n_samples = len(d) // 2
            samples = list(struct.unpack(f">{n_samples}h", d[:n_samples*2]))
            fname = f"decode_{label}_s16be_8k.wav"
            save_wav_16bit(fname, samples, 8000)
            results.append(fname)

        # 7. audioop u-law decode (Python built-in)
        try:
            pcm = audioop.ulaw2lin(d, 2)
            fname = f"decode_{label}_audioop_ulaw_8k.wav"
            with wave.open(fname, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(pcm)
            dur = len(d) / 8000
            print(f"  Saved {fname} ({dur:.1f}s, audioop u-law)")
            results.append(fname)
        except Exception as e:
            print(f"  audioop u-law failed: {e}")

        # 8. audioop A-law decode
        try:
            pcm = audioop.alaw2lin(d, 2)
            fname = f"decode_{label}_audioop_alaw_8k.wav"
            with wave.open(fname, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(8000)
                wf.writeframes(pcm)
            dur = len(d) / 8000
            print(f"  Saved {fname} ({dur:.1f}s, audioop A-law)")
            results.append(fname)
        except Exception as e:
            print(f"  audioop A-law failed: {e}")

    return results


def analyze_chunks(data, header_len):
    """Try to identify chunks / interleaved metadata in the stream."""
    print("\n" + "=" * 60)
    print("CHUNK/INTERLEAVING ANALYSIS")
    print("=" * 60)

    audio = data[header_len:]

    # Look for bytes with high bit set that might be tags embedded in the stream
    tag_positions = [i for i in range(len(audio)) if audio[i] & 0x80 and audio[i] != 0xFF and audio[i] != 0xFE and audio[i] != 0xFD]
    print(f"\n  Bytes with 0x80+ (excluding 0xFD-0xFF) in first 200 bytes:")
    for pos in tag_positions[:30]:
        if pos < 200:
            context = audio[max(0,pos-2):pos+5]
            ctx_hex = " ".join(f"{b:02X}" for b in context)
            print(f"    offset {pos}: 0x{audio[pos]:02X}  context: {ctx_hex}")

    # Check if 0x01 appears periodically as a potential frame marker
    marker_scan = data[0]  # First byte of stream
    positions = []
    for i in range(len(data)):
        if data[i] == marker_scan:
            positions.append(i)
        if i > 20000:
            break

    if len(positions) > 5:
        diffs = [positions[i+1] - positions[i] for i in range(min(40, len(positions)-1))]
        from collections import Counter
        common = Counter(diffs).most_common(10)
        print(f"\n  Byte 0x{marker_scan:02X} occurrences: {len(positions)} in first 20KB")
        print(f"  Most common spacings: {common}")

        # Check for the most common spacing being consistent
        if common[0][1] > len(positions) * 0.3:
            chunk_size = common[0][0]
            print(f"\n  >>> POSSIBLE CHUNK SIZE: {chunk_size} bytes")
            print(f"      At 8kHz, {chunk_size} bytes = {chunk_size/8:.1f}ms per chunk")

            # Extract and show first few chunk headers
            for j in range(min(5, len(positions))):
                pos = positions[j]
                chunk_hdr = data[pos:pos+8]
                print(f"      Chunk {j} at offset {pos}: {' '.join(f'{b:02X}' for b in chunk_hdr)}")


def correlation_analysis(data, header_len):
    """Check sample-to-sample correlation (real audio has high correlation)."""
    print("\n" + "=" * 60)
    print("AUTOCORRELATION CHECK")
    print("=" * 60)

    d = data[header_len:header_len+4000]
    if len(d) < 100:
        print("  Not enough data")
        return

    # 8-bit interpretation
    diffs_8bit = [abs(d[i+1] - d[i]) for i in range(len(d)-1)]
    avg_diff_8bit = sum(diffs_8bit) / len(diffs_8bit)
    print(f"\n  8-bit unsigned interpretation:")
    print(f"    Average sample-to-sample difference: {avg_diff_8bit:.1f}")
    print(f"    (Real 8kHz audio: typically 2-15, random noise: ~85)")

    # 16-bit LE interpretation
    if len(d) >= 4:
        n = len(d) // 2
        samples = struct.unpack(f"<{n}h", d[:n*2])
        diffs_16 = [abs(samples[i+1] - samples[i]) for i in range(n-1)]
        avg_diff_16 = sum(diffs_16) / len(diffs_16)
        print(f"\n  16-bit LE signed interpretation:")
        print(f"    Average sample-to-sample difference: {avg_diff_16:.1f}")
        print(f"    (Real 8kHz audio: typically 100-2000, random noise: ~21000)")

    # 16-bit BE interpretation
    if len(d) >= 4:
        samples = struct.unpack(f">{n}h", d[:n*2])
        diffs_16 = [abs(samples[i+1] - samples[i]) for i in range(n-1)]
        avg_diff_16 = sum(diffs_16) / len(diffs_16)
        print(f"\n  16-bit BE signed interpretation:")
        print(f"    Average sample-to-sample difference: {avg_diff_16:.1f}")
        print(f"    (Real 8kHz audio: typically 100-2000, random noise: ~21000)")

    # u-law decode then check correlation
    ulaw_table = ulaw_decode_table()
    samples = [ulaw_table[b] for b in d]
    diffs_ulaw = [abs(samples[i+1] - samples[i]) for i in range(len(samples)-1)]
    avg_diff_ulaw = sum(diffs_ulaw) / len(diffs_ulaw)
    print(f"\n  u-law decoded interpretation:")
    print(f"    Average sample-to-sample difference: {avg_diff_ulaw:.1f}")
    print(f"    (Real audio: typically 100-2000, random noise: ~10000+)")


def main():
    print("=" * 60)
    print("WebSDR Stream Decoder")
    print("=" * 60)

    if not os.path.exists(DUMP_FILE):
        print(f"ERROR: {DUMP_FILE} not found. Run analyze_websdr.py first.")
        return

    with open(DUMP_FILE, "rb") as f:
        data = f.read()

    print(f"Loaded {len(data)} bytes from {DUMP_FILE}")

    # Parse header
    header_len, params = parse_header(data)

    # Autocorrelation analysis (tells us which interpretation has real audio)
    correlation_analysis(data, header_len)

    # Chunk analysis
    analyze_chunks(data, header_len)

    # Try all decodings
    print("\n" + "=" * 60)
    print("DECODING ATTEMPTS — WAV FILES")
    print("=" * 60)
    results = try_all_decodings(data, header_len)

    # Summary
    print("\n" + "=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    print(f"\n  Generated {len(results)} WAV files. Play them to find the correct decoding:")
    for f in results:
        print(f"    start {f}")
    print(f"\n  The one that sounds like radio audio (not noise) is the correct format.")
    print(f"  Tell me which file sounds right and I'll update the ESP32 firmware.")


if __name__ == "__main__":
    main()
