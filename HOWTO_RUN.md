# How to Run the UFONet Hardware Test Suite

Use Raspberry Pi Imager, Balena Etcher, or similar to flash UFONetTestHardware.img.xz to a microSD card of at least ~8GB capacity.

Insert the microSD into the Raspberry Pi 4 and ensure the Pi 4 is connected to all hardware (WittyPi 4 power manager, Flir Lepton, AHT20, BNO055, etc.,)

Press the power button on the WittyPi 4 to turn the system on. Allow time for the filesystem to expand. Connect via serial UART adapter or connect a keyboard and HDMI display. 

Log in with pi / hydrology

Run the test script with python ./TestHardware/test_watercam.py 

Issues with hardware or wiring will cause errors in output.

## System Details - Not needed with prepared SD file image

**System packages** (Raspberry Pi OS Bookworm/Trixie):
```bash
sudo apt install python3-picamera2 python3-rpi.gpio i2c-tools
```

**Python packages:**
```bash
pip install -r requirements.txt
```

**Required `/boot/firmware/config.txt` overlays** (verify before running):
```
dtparam=i2c_arm=on
dtparam=spi=on
dtoverlay=uart5
```

---

## Main test harness — `test_watercam.py`

Covers all hardware in one pass: system, I2C bus, SPI bus, WittyPi 4, AHT20, BNO055, FLIR Lepton 3.5, Multitech mDot, and IR-CUT camera.

**GPIO and SPI tests require root:**
```bash
sudo python3 test_watercam.py
```

### Common invocations

| Command | Effect |
|---|---|
| `sudo python3 test_watercam.py` | Run all tests |

### Available `--test` values
`system`, `i2c`, `spi`, `wittypi`, `aht20`, `bno055`, `lepton`, `mdot`, `ircut`

### Exit codes
- `0` — all tests passed
- `1` — one or more tests failed

---

## IR-CUT filter functional test — `test_ircut_filter.py`

Separately verifies the filter physically moves by comparing image brightness with GPIO21 LOW vs. HIGH. Requires a scene with natural or halogen light (LED-only panels have too little IR).

```bash
sudo python3 test_ircut_filter.py
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--save-images` | off | Write PNG files from each GPIO state for visual inspection |
| `--gpio PIN` | 21 | Override BCM GPIO pin |
| `--threshold N` | 0.20 | Minimum relative brightness change to pass (e.g. `0.15` for dimmer scenes) |

### Example
```bash
sudo python3 test_ircut_filter.py --save-images --threshold 0.15
```

### Pass criteria
1. Brightness increases ≥ threshold when filter is removed (GPIO HIGH)
2. Brightness returns toward baseline when GPIO returns LOW

---

## Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| GPIO/SPI tests fail | Re-run with `sudo` |
| `/dev/ttyAMA5` not found | Add `dtoverlay=uart5` to `config.txt`, reboot |
| `/dev/spidev0.0` not found | Enable SPI overlay, reboot |
| mDot no response after retries | Module is mid-join; wait ~30 s and retry |
| Lepton boot timeout | Allow 1–2 s after power-on; check GPIO6 wiring |
| IR-CUT brightness change < 20 % | Use sunlight or halogen; try `--threshold 0.10` |
