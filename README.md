# UFOTestSuite

Post-assembly hardware validation for WaterCam sensor nodes.  Run the test harness on a freshly-built unit before sealing it into its enclosure and deploying it in the field.

---

## Table of Contents

- [Quick Start (end user)](#quick-start-end-user)
- [What the tests check](#what-the-tests-check)
- [Interpreting results and troubleshooting failures](#interpreting-results-and-troubleshooting-failures)
- [Dependencies](#dependencies)
- [Developer guide](#developer-guide)
  - [Repository layout](#repository-layout)
  - [Code structure](#code-structure)
  - [Hardware reference](#hardware-reference)
  - [Adding a new test](#adding-a-new-test)
  - [Known issues and hardware quirks](#known-issues-and-hardware-quirks)

---

## Quick Start (end user)

These steps assume you have a fully assembled WaterCam unit powered on and connected to a network or serial console, running Raspberry Pi OS Trixie (Debian 13), with the SU-WaterCam software image already flashed.

**1. Get the test script onto the device.**

If the device has internet access:

```bash
cd /home/pi
git clone https://github.com/mandeeps/UFOTestSuite.git
```

Or copy it over the network with `scp`:

```bash
scp watercam_hardware_test.py pi@<device-ip>:/home/pi/
```

**2. Run all tests.**

```bash
sudo /home/pi/SU-WaterCam/venv/bin/python /home/pi/UFOTestSuite/watercam_hardware_test.py
```

Root is required for GPIO and SPI access.  The script will not damage the hardware — it reads sensors, pulses the Lepton reset line briefly (< 200 ms), toggles the IR-CUT filter GPIO, and sends a few read-only AT commands to the mDot.

**3. Read the summary.**

At the end of the run you will see a table like:

```
============================================================
  SUMMARY
============================================================
  system       9/9
  i2c          5/5
  spi          2/2
  wittypi      6/6
  aht20        3/3
  bno055       7/7
  lepton       9/9
  mdot         7/7
  ircut        4/4

  TOTAL: 52 passed, 0 failed
```

A fully assembled and correctly configured unit should pass every test.  Any `FAIL` line prints the component name, test name, and a short explanation directly below the summary.

**4. Optional: run a single component.**

Useful when re-checking one part after a repair:

```bash
sudo python3 watercam_hardware_test.py --test lepton
sudo python3 watercam_hardware_test.py --test mdot --verbose
```

Available component names: `system`, `i2c`, `spi`, `wittypi`, `aht20`, `bno055`, `lepton`, `mdot`, `ircut`.

**5. Capture output to a file for records.**

```bash
sudo python3 watercam_hardware_test.py --no-color 2>&1 | tee test-$(hostname)-$(date +%Y%m%d).txt
```

---

## What the tests check

### `system` — OS and interfaces

| Test | Pass condition |
|------|---------------|
| hostname readable | `hostname` command returns a non-empty string |
| kernel version | `uname -r` succeeds |
| Python ≥ 3.11 | Runtime is Python 3.11 or newer (Trixie default) |
| Raspberry Pi hardware | `/proc/device-tree/model` contains "Raspberry Pi" |
| CPU temperature < 80 °C | `vcgencmd measure_temp` returns a value below 80 °C |
| device `/dev/i2c-1` | I2C bus 1 device node exists (requires `dtparam=i2c_arm=on`) |
| device `/dev/spidev0.0` | SPI bus device node exists (requires `dtparam=spi=on`) |
| device `/dev/ttyAMA5` | UART5 device node exists (requires `dtoverlay=uart5`) |
| rpicam detects cameras | `rpicam-hello --list-cameras` finds at least one camera |

### `i2c` — I2C bus scan

Runs `i2cdetect -y 1` and checks that each expected device responds at its documented address.

| Address | Device |
|---------|--------|
| `0x08` | WittyPi 4 MCU (unified virtual address; LM75B and RTC are on its internal bus) |
| `0x28` | Adafruit BNO055 IMU (default address; `0x29` if ADR pin pulled high) |
| `0x38` | Adafruit AHT20 temperature/humidity |
| `0x2A` | FLIR Lepton CCI (command/control interface) |
| `0x50` | HAT EEPROM (optional; reported but not required) |

If an address is missing, the physical connection to that device is broken, the device was not powered up, or the kernel I2C driver is not loaded.

### `spi` — SPI bus

Checks that `/dev/spidev0.0` exists, opens the device, performs a short `xfer2`, and closes it.  This confirms the kernel SPI driver is loaded.  It does not verify data integrity (there is no SPI loopback on the PCB); real data integrity is verified under the `lepton` tests via VOSPI.

### `wittypi` — WittyPi 4 power management HAT

Uses the WittyPi shell utilities (`/home/pi/wittypi/utilities.sh`) to query the board over I2C.

| Test | Pass condition |
|------|---------------|
| utilities.sh present | `/home/pi/wittypi/utilities.sh` file exists |
| board temperature in range | LM75B reads −20 °C to +85 °C |
| battery voltage ≥ 3.6 V | Input voltage from the Voltaic pack is at least 3.6 V |
| output voltage 4.75–5.25 V | 5 V rail to the Pi is within ±5 % |
| RTC has valid year (20xx) | RTC time string contains a four-digit year starting with "20" |
| GPIO4 SYS_UP reads HIGH | WittyPi GPIO4 (SYS_UP) is HIGH, confirming the Pi signalled it is running |

A low battery voltage reading means the Voltaic pack is nearly empty or not connected.  An output voltage outside 4.75–5.25 V suggests a WittyPi fault or the load is too high for the supply.

### `aht20` — Adafruit AHT20 temperature and humidity

Uses the Adafruit CircuitPython driver (`adafruit_ahtx0`) to read the sensor over I2C.

| Test | Pass condition |
|------|---------------|
| temperature reading in range | −40 °C to +85 °C (sensor operating range) |
| humidity reading in range | 0 % to 100 % |
| both readings plausible | Temperature below 70 °C and humidity above 0 % |

The AHT20 monitors the internal environment of the sealed enclosure.  Readings that are wildly wrong at room temperature indicate a wiring fault or damaged sensor.

### `bno055` — Adafruit BNO055 IMU

Uses the Adafruit CircuitPython driver (`adafruit_bno055`).  The BNO055 is an absolute-orientation sensor; the WaterCam uses it to include tilt/roll/yaw in each LoRa packet so the flood-detection model can correct for camera angle changes.

| Test | Pass condition |
|------|---------------|
| I2C communication established | Driver initializes without exception |
| Euler angles readable | Returns a 3-tuple (heading, roll, pitch) |
| Acceleration readable | Returns a 3-tuple in m/s² |
| Gyroscope readable | Returns a 3-tuple in rad/s |
| Quaternion readable | Returns a 4-tuple |
| die temperature in range | Internal thermometer reads −20 °C to +80 °C |
| calibration status readable | Returns (sys, gyro, accel, mag) calibration scores 0–3 |

Calibration scores below 3 are normal immediately after power-on.  The sensor will self-calibrate during normal operation.  Use `--verbose` to see calibration numbers and a hint if scores are low.

### `lepton` — FLIR Lepton 3.5 thermal camera

The most complex component to verify.  Tested in three stages.

**Stage 1 — GPIO reset (GPIO6 / BCM, physical pin 31)**

Pulses RESET_L low for 100 ms then releases it and waits 300 ms for boot.  This verifies the reset line wiring and leaves the Lepton in a known state for the I2C and SPI tests that follow.

**Stage 2 — CCI I2C (address 0x2A)**

Reads the Lepton STATUS register (CCI register 0x0002) using 16-bit addressed I2C transactions (`smbus2`).

| Test | Pass condition |
|------|---------------|
| CCI I2C communication (0x2A) | I2C transaction completes without exception |
| Lepton boot complete (STATUS.boot=1) | Bit 1 of STATUS is 1 (normal operation mode) |
| Lepton not busy | Bit 0 of STATUS is 0 |
| Lepton no error | Bits 15:8 of STATUS are 0x00 |

If the CCI address (`0x2A`) does not appear in the `i2c` scan, the Lepton Breakout v2 J2 connector is wired incorrectly.  Refer to the PCB design changes document (`../pcb-designs/WaterCam_PCB_Design_Changes_Required.md`) — the v6.0 PCB had critical J2 pinout errors.

**Stage 3 — VOSPI SPI stream**

Opens `/dev/spidev0.0` at 16 MHz, SPI mode 3, and reads up to 500 packets of 164 bytes each.  Valid packets have bits [15:12] of the first word not equal to `0xF` (the Lepton uses `0xF` for "discard" packets between frames).  Finding at least 10 valid packets confirms the VOSPI data stream is active.

**Stage 4 — binaries**

Checks that `/home/pi/SU-WaterCam/lepton` and `/home/pi/SU-WaterCam/capture` exist and are executable.  These are the C programs compiled from `tools/lepton.c` and `tools/capture.c` that the main runtime uses to save thermal image and temperature data.

### `mdot` — Multitech mDot LoRa module

Communicates over UART5 (`/dev/ttyAMA5`, 115200 8N1) using AT commands.

| Test | Pass condition |
|------|---------------|
| serial port opened | `serial.Serial()` opens without exception |
| AT → OK | mDot echoes `OK` to the bare `AT` command |
| AT+VER firmware version | Returns firmware string and `OK` |
| AT+DI device EUI readable | Returns an 8-byte EUI-64 and `OK` |
| AT+NJS join status readable | Returns join status (0=not joined, 1=joined) and `OK` |
| AT+TXP TX power readable | Returns current TX power setting and `OK` |
| AT+RSSI signal quality readable | Returns RSSI/SNR (or `ERROR` if not yet joined — both are acceptable) |

If `AT → OK` fails: check that `dtoverlay=uart5` is in `/boot/firmware/config.txt`, that `/dev/ttyAMA5` exists, and that the TX/RX wires to the mDot PA_2/PA_3 pads are correctly crossed (mDot TX → Pi RX, mDot RX → Pi TX).

### `ircut` — IR-CUT camera (Dorhea / CSI)

| Test | Pass condition |
|------|---------------|
| rpicam detects CSI camera | `rpicam-hello --list-cameras` shows camera 0 |
| picamera2 import | `from picamera2 import Picamera2` succeeds |
| test capture successful | 640×480 JPEG written to a temp file, file size > 1 KB |
| GPIO21 IR-CUT filter control | GPIO21 can be set HIGH and LOW without exception |

The IR-CUT filter is controlled by a wire soldered to the camera board and connected to GPIO21 (BCM), physical pin 40.  `LOW` = normal visible-light photo; `HIGH` = NIR-inclusive photo.  The camera must have been hardware-modified before this test is meaningful — see the SU-WaterCam build guide.

---

## Interpreting results and troubleshooting failures

### All `i2c` tests fail

The I2C bus is not enabled.  Check `/boot/firmware/config.txt` for `dtparam=i2c_arm=on` and reboot.  If the overlay is present but devices are still missing, run `i2cdetect -y 1` manually and compare the output against the address table above.

### `0x2A` (Lepton CCI) missing from I2C scan

The most likely cause on assembled PCBs is the J2 connector pinout error in PCB v6.0.  The SDA line is incorrectly wired to GND on that board revision.  See `../pcb-designs/WaterCam_PCB_Design_Changes_Required.md`, issue #1.

Verify by temporarily connecting the Lepton Breakout with jumper wires following the correct pinout from FLIR document 250-0577-24 and re-running `--test i2c`.

### Lepton VOSPI delivers only discard packets

The Lepton requires approximately 185 ms after reset before the VOSPI stream becomes valid.  If the reset test passes but VOSPI only shows discard packets, wait a few seconds and re-run `--test lepton`.  If it persists, check the SPI wiring (MISO/MOSI not swapped, correct CS line, mode 3).

### `mDot` — `AT → OK` fails, port opens fine

The most common causes:
1. `dtoverlay=uart5` is missing from `/boot/firmware/config.txt`.
2. TX and RX are swapped (mDot PA_2 is TX from the mDot's perspective; it should connect to the Pi's RXD5 / GPIO13).
3. The mDot is in a non-default baud rate.  Try `AT+IPR?` at various baud rates or perform a factory reset by holding the mDot's RESET pin low for 10 s.

### WittyPi battery voltage unexpectedly low

The Voltaic V50/V75 pack may be depleted or the dual USB-A → USB-C Y-cable may not be fully inserted.  Each USB-A port on Voltaic packs provides up to 2.5 A; the Y-cable combines them for the 3 A the WittyPi needs.  Charge the battery pack and re-test.

### BNO055 calibration all zeros

This is normal on first power-on.  The BNO055 loses calibration when power is removed unless offsets are saved and restored in software.  Move the unit through a figure-8 pattern a few times; calibration scores will rise.  Calibration does not affect the `bno055` tests (they only verify the sensor is wired and communicating).

### GPIO tests fail with `RuntimeError: No access to /dev/mem`

Re-run with `sudo`.

---

## Dependencies

All of the following should be present on the standard SU-WaterCam SD card image.  If running on a fresh Trixie install:

```bash
# System packages
sudo apt install -y i2c-tools python3-smbus2 python3-spidev \
    python3-rpi.gpio python3-picamera2 libcamera-apps python3-serial

# Adafruit CircuitPython drivers (in the SU-WaterCam venv)
source /home/pi/SU-WaterCam/venv/bin/activate
pip install adafruit-circuitpython-ahtx0 adafruit-circuitpython-bno055 smbus2 spidev
```

Required `/boot/firmware/config.txt` overlays:

```ini
dtparam=i2c_arm=on
dtparam=spi=on
dtoverlay=uart5
# Optional: slow I2C for BNO055 clock stretching if sensor gives errors
# dtparam=i2c_arm_baudrate=10000
```

---

## Developer guide

### Repository layout

```
UFOTestSuite/
├── README.md                    ← this file
└── watercam_hardware_test.py    ← the test harness
```

Companion repositories (expected at the same level as this repo):

```
SU-WaterCam/          ← main sensor runtime, sensor drivers, AT command logic
pcb-designs/          ← KiCad schematic and PCB for the sensor HAT
WittyPi4Python/       ← WittyPi 4 Python interface (vendored into SU-WaterCam/tools/)
```

### Code structure

`watercam_hardware_test.py` is a single self-contained file with no project-specific imports.  It uses only standard library modules plus the hardware drivers that must already be installed on the device.

**Key globals**

| Name | Purpose |
|------|---------|
| `USE_COLOR` | Controls ANSI escape codes; set to `False` by `--no-color` |
| `RESULTS` | List of `dict` records accumulated by every `record()` call |
| `COMPONENTS` | `dict[str, Callable]` mapping component names to test functions |
| `LEPTON_RESET_GPIO` | BCM pin number for Lepton RESET_L (currently 6) |
| `LEPTON_I2C_ADDR` | CCI I2C address (0x2A) |
| `MDOT_PORT / MDOT_BAUD` | Serial port path and baud rate for mDot |
| `IRCUT_GPIO_BCM` | BCM pin for IR-CUT filter control (currently 21; see known issues) |

**Helper functions**

| Function | Purpose |
|----------|---------|
| `record(component, name, passed, detail, value)` | Append a result and print the PASS/FAIL line |
| `section(title)` | Print a section header |
| `warn(msg)` | Print a yellow WARN line (does not add a result entry) |
| `safe(component, name, fn, ...)` | Wrap any callable; catches exceptions and calls `record(..., False)` |
| `print_summary()` | Print the per-component table and failure list; returns `True` if all passed |

**Test functions** — one per component, all have the signature `(verbose: bool) -> None`.  They call `record()` directly; no return value is needed.

**`_i2c_scan()`** — parses `i2cdetect -y 1` output into a `set[int]` of responding addresses.

**`_mdot_cmd(ser, cmd)`** — writes an AT command, reads lines until `OK` or `ERROR` appears or the timeout expires, returns the joined response.

**`_lepton_i2c_read(bus, addr, reg, length)`** — performs a CCI-style 16-bit-addressed I2C read using `smbus2.i2c_rdwr`.  Not currently called directly in the test (the test builds the messages inline), but available as a utility for future CCI register reads.

### Hardware reference

#### GPIO allocation (BCM numbering)

| BCM GPIO | Physical pin | Function | Notes |
|----------|-------------|----------|-------|
| 2 / SDA1 | 3 | I2C SDA | All I2C devices share this |
| 3 / SCL1 | 5 | I2C SCL | |
| 4 | 7 | WittyPi SYS_UP input | **Never drive this pin** |
| 6 | 31 | Lepton RESET_L | Active-low; pull HIGH for normal operation |
| 8 / CE0 | 24 | SPI CS0 | Lepton VOSPI chip select |
| 9 / MISO | 21 | SPI MISO | Lepton VOSPI data out |
| 10 / MOSI | 19 | SPI MOSI | Lepton VOSPI data in (unused in practice) |
| 11 / SCLK | 23 | SPI clock | Lepton VOSPI clock |
| 12 / TXD5 | 32 | UART5 TX → mDot RX | Requires `dtoverlay=uart5` |
| 13 / RXD5 | 33 | UART5 RX ← mDot TX | |
| 17 | 11 | WittyPi SYS_UP | **Never drive this pin** |
| 21 | 40 | IR-CUT filter control | |

#### I2C address map

| Address | Device | Notes |
|---------|--------|-------|
| 0x08 | WittyPi 4 MCU | Unified virtual address; LM75B (0x48) and RTC (0x51) are on its internal bus |
| 0x28 | BNO055 (default) | 0x29 if ADR pin pulled high |
| 0x38 | AHT20 | Fixed |
| 0x2A | FLIR Lepton CCI | Fixed |
| 0x50 | HAT EEPROM | Optional |

No address conflicts exist in the default configuration.

#### Serial interfaces

| Port | Device | Baud | Protocol |
|------|--------|------|---------|
| `/dev/ttyAMA5` | Multitech mDot | 115200 8N1 | AT commands (UART5, GPIO12/13) |

#### SPI

| Bus | Device | Mode | Max speed | Protocol |
|-----|--------|------|-----------|---------|
| spidev0.0 | FLIR Lepton VOSPI | 3 | 16 MHz | VOSPI (164-byte packets) |

### Adding a new test

1. Write a function `test_<component>(verbose: bool) -> None`.  Call `section()` at the top, then call `record()` for each assertion.

2. Add it to the `COMPONENTS` dict near the bottom of the file:

   ```python
   COMPONENTS = {
       ...
       "mydevice": test_mydevice,
   }
   ```

3. The function will automatically appear in `--test` tab-completion and will run as part of the default full suite.

Keep each `record()` call to a single, specific assertion.  Prefer many narrow checks over one broad one — narrow checks make failure diagnosis faster.

Use `warn()` for advisory information that does not constitute a test failure (e.g. "calibration is low but the sensor is communicating correctly").

If a test needs root access, note it in a comment; the harness already warns the user if not running as root.

If a hardware dependency is missing (e.g. a device node or a Python package), fail gracefully with a `record(..., False, "explanation")` rather than raising an exception.

### Known issues and hardware quirks

#### PCB v6.0 J2 connector pinout errors

The Lepton Breakout v2 J2 connector has almost all its SPI and I2C signals on wrong pins in the v6.0 PCB schematic.  A unit built with the v6.0 PCB will fail the `lepton` CCI I2C test and the `i2c` address 0x2A test.  Temporary fix: connect the Lepton via jumper wires using the correct pinout from FLIR document 250-0577-24.  Permanent fix: redesign J2 per `../pcb-designs/WaterCam_PCB_Design_Changes_Required.md`.

#### GPIO17 conflict (WittyPi SYS_UP vs Lepton GPIO2/VSYNC)

The v6.0 PCB routes Lepton J2-P15 to GPIO17, which is exclusively owned by the WittyPi SYS_UP signal.  The test harness never drives GPIO17.  This is a PCB defect; the corrected design routes J2-P15 to GPIO24.

#### mDot 3.3 V supply

The mDot must remain powered when the Raspberry Pi is off.  Its role is to receive an inbound LoRa wake command and assert the WittyPi SW line (via Q1) to start the Pi — if the mDot loses power when the Pi shuts down, this wake path is broken.

The v6.0 PCB connects mDot VDD to the WittyPi P3-3V3 pin.  This is the WittyPi's internal MCU/RTC supply rail, which **is** always-on (stays live whenever the battery is connected), making it the correct power domain.  The concern with this rail is current capacity: the mDot draws up to ~127 mA peak during LoRa TX.  The WittyPi's MCU/RTC rail is not intended to supply this.  The fix is to add adequate bulk decoupling at the mDot VDD pin (100 µF electrolytic + 100 nF ceramic) so that TX current spikes are sourced locally from the capacitors rather than drawn from the WittyPi rail.

**The Pi's 3.3 V GPIO rail (pins 1 or 17) must not be used** — it is switched off whenever the Pi is powered down, which breaks the remote wake path entirely.

If the mDot is unresponsive on a v6.0 board, verify the P3-3V3 connection and check that decoupling capacitors are fitted.

#### WittyPi shell utility timeout on slow I2C

If `get_temperature` or other WittyPi utility commands time out (returning `None`), I2C may be running too slow due to clock stretching from the BNO055.  The `i2c_arm_baudrate=10000` overlay slows the bus to accommodate the BNO055 but can cause WittyPi utilities to time out.  The test harness uses a 5 s subprocess timeout; increase it in `run_wittypi()` if needed.

#### VOSPI discard packets on first read

The Lepton outputs discard packets (`0xF` ID nibble) between frames and for approximately 185 ms after reset.  The test reads up to 500 packets looking for 10 valid ones; this is sufficient for normal operation but may not be enough if the Pi is heavily loaded and the SPI read loop runs slowly.  If VOSPI tests are marginal, re-run `--test lepton` after a fresh boot.
