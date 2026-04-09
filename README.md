# ATS Mini — WebSDR Fork

![](docs/source/_static/esp32-si4732-ui-theme.jpg)

This is a fork of the [ATS-Mini](https://github.com/esp32-si4732/ats-mini) firmware for the SI4732 (ESP32-S3) Mini/Pocket Receiver, with **WebSDR client** and **on-device WiFi Setup** added.

## Added Features

### WebSDR Client
Listen to remote HF receivers over the internet directly from your pocket radio — no PC needed.

- Connects to the **Maasbree WebSDR** (Netherlands) via WiFi
- **8 HF bands**: 160m, 80m, 60m, 40m, 30m, 20m, 17m, 15m
- **5 modulation modes**: AM, LSB, USB, CW, FM
- Real-time audio streaming with adaptive predictive codec
- S-meter display from the remote receiver
- Dedicated full-screen UI with rotary encoder navigation (TUNE / BAND / MODE / VOL / EXIT)
- Dual-core architecture: Core 0 handles network + audio decode, Core 1 handles UI
- ~512ms audio pre-buffer to absorb WiFi jitter

Access from: **Menu → WebSDR**

### On-Device WiFi Setup
Manage WiFi networks directly on the radio — no web browser needed.

- **Scan** for available networks with signal strength indicators
- **Select** a network and enter password using the rotary encoder
- **Forget** saved networks with one click
- Credentials saved to NVS flash (survives power cycles)
- Supports up to 3 saved networks
- Compatible with the existing web UI configuration

Access from: **Settings → WiFi Setup**

### Smooth Micro Stepping
Tuning feels smoother with micro stepping — large frequency steps are broken into smaller incremental moves.

- **SSB**: A 100 Hz step is applied as 10 × 10 Hz micro steps via BFO adjustments
- **AM/FM**: Similarly subdivided into 1/10th increments
- Steps animate smoothly when the encoder is idle, or jump instantly during fast rotation
- No user configuration needed — works automatically with any step size

## Flashing

Download the firmware binaries from the [Releases](https://github.com/ivanessb/ATS-MINI-websdr/releases) page:

| File | Description | Flash Offset |
|------|-------------|-------------|
| `ats-mini.ino.merged.bin` | Full flash image (recommended for fresh install) | `0x0` |
| `ats-mini.ino.bin` | Application firmware only | `0x10000` |
| `ats-mini.ino.bootloader.bin` | Bootloader | `0x0` |
| `ats-mini.ino.partitions.bin` | Partition table | `0x8000` |

For detailed flashing instructions, see the [original documentation](https://esp32-si4732.github.io/ats-mini/flash.html).

## Building from Source

Requires [arduino-cli](https://arduino.github.io/arduino-cli/) and the ESP32 board package.

```bash
# Install dependencies
arduino-cli core install esp32:esp32@3.3.7 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install "PU2CLR SI4735@2.1.8" "TFT_eSPI@2.5.43" "Async TCP@3.4.7" "ESP Async WebServer@3.7.10" "NTPClient@3.2.1"

# Build
arduino-cli compile --profile esp32s3-ospi ats-mini

# Upload (replace COM5 with your port)
arduino-cli compile --upload --port COM5 --profile esp32s3-ospi ats-mini
```

## Original Project

Based on the following sources:

* Volos Projects:    https://github.com/VolosR/TEmbedFMRadio
* PU2CLR, Ricardo:   https://github.com/pu2clr/SI4735
* Ralph Xavier:      https://github.com/ralphxavier/SI4735
* Goshante:          https://github.com/goshante/ats20_ats_ex
* G8PTN, Dave:       https://github.com/G8PTN/ATS_MINI
* Original firmware: https://github.com/esp32-si4732/ats-mini

## Documentation

The original hardware, software and flashing documentation is available at <https://esp32-si4732.github.io/ats-mini/>
