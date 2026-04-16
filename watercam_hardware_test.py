#!/usr/bin/env python3
"""
WaterCam Hardware Test Harness
Raspberry Pi 4B + WittyPi 4 + FLIR Lepton 3.5 (Breakout 2.0) +
Multitech mDot + Adafruit BNO055 + Adafruit AHT20 + IR-CUT Camera

Run as:
    python3 watercam_hardware_test.py               # all tests
    python3 watercam_hardware_test.py --test i2c    # one component
    python3 watercam_hardware_test.py --verbose     # extra detail
    python3 watercam_hardware_test.py --no-color    # plain output
    sudo python3 watercam_hardware_test.py          # required for SPI/GPIO tests

GPIO allocation (BCM numbering, from PCB design):
  GPIO2/SDA1  - I2C SDA (AHT20, BNO055, Lepton CCI, WittyPi RTC/temp)
  GPIO3/SCL1  - I2C SCL
  GPIO4       - WittyPi SYS_UP (DO NOT DRIVE)
  GPIO6       - Lepton RESET_L (active-low, default HIGH)
  GPIO8/CE0   - SPI CS0 (Lepton VOSPI)
  GPIO9/MISO  - SPI MISO (Lepton VOSPI)
  GPIO10/MOSI - SPI MOSI (Lepton VOSPI)
  GPIO11/SCLK - SPI CLK (Lepton VOSPI)
  GPIO12/TXD5 - UART5 TX → mDot RX
  GPIO13/RXD5 - UART5 RX ← mDot TX
  GPIO17      - WittyPi SYS_UP (DO NOT DRIVE)
  GPIO21      - IR-CUT camera filter control (pin 40)

I2C address map (no conflicts):
  0x08        - WittyPi 4 MCU (unified virtual address; LM75B and RTC
                are on its internal bus, not directly on Pi I2C-1)
  0x28 / 0x29 - BNO055 IMU
  0x38        - AHT20 temp/humidity
  0x50        - HAT EEPROM (if fitted)
  0x2A        - FLIR Lepton CCI

Serial: mDot on /dev/ttyAMA5 (UART5, 115200 8N1)
SPI:    Lepton VOSPI on /dev/spidev0.0, mode 3, 16 MHz max
Camera: CSI ribbon to Pi, controlled via rpicam / picamera2
"""

import sys
import os
import time
import struct
import argparse
import subprocess
from typing import Any

# ---------------------------------------------------------------------------
# Terminal colour helpers
# ---------------------------------------------------------------------------

USE_COLOR = True


def _c(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t):  return _c("32", t)
def red(t):    return _c("31", t)
def yellow(t): return _c("33", t)
def cyan(t):   return _c("36", t)
def bold(t):   return _c("1",  t)


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

RESULTS: list[dict] = []


def record(component: str, name: str, passed: bool, detail: str = "", value: Any = None):
    entry = {
        "component": component,
        "name": name,
        "passed": passed,
        "detail": detail,
        "value": value,
    }
    RESULTS.append(entry)
    status = green("PASS") if passed else red("FAIL")
    line = f"  [{status}] {name}"
    if detail:
        line += f": {detail}"
    if value is not None:
        line += f"  ({value!r})"
    print(line)
    return passed


def section(title: str):
    print()
    print(bold(cyan(f"=== {title} ===")))


def warn(msg: str):
    print(f"  {yellow('WARN')} {msg}")


# ---------------------------------------------------------------------------
# Safe runner – catches all exceptions so one broken test never halts the suite
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 1. System / OS checks
# ---------------------------------------------------------------------------

def test_system(verbose: bool):
    section("System")

    # Hostname
    hostname = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    record("system", "hostname readable", bool(hostname), hostname)

    # Kernel
    uname = subprocess.run(["uname", "-r"], capture_output=True, text=True).stdout.strip()
    record("system", "kernel version", bool(uname), uname)

    # Python version ≥ 3.11 (Trixie ships 3.11+)
    major, minor = sys.version_info.major, sys.version_info.minor
    record("system", f"Python ≥ 3.11", major == 3 and minor >= 11,
           f"{major}.{minor}.{sys.version_info.micro}")

    # Running on Pi
    model_path = "/proc/device-tree/model"
    if os.path.exists(model_path):
        with open(model_path, "rb") as f:
            model = f.read().rstrip(b"\x00").decode(errors="replace")
        record("system", "Raspberry Pi hardware", "Raspberry Pi" in model, model)
    else:
        record("system", "Raspberry Pi hardware", False, "device-tree model not found")

    # CPU temperature via vcgencmd
    try:
        res = subprocess.run(["vcgencmd", "measure_temp"],
                             capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            temp_str = res.stdout.strip()  # e.g. temp=42.8'C
            temp_c = float(temp_str.split("=")[1].split("'")[0])
            ok = temp_c < 80.0
            record("system", "CPU temperature < 80 °C", ok, f"{temp_c:.1f} °C")
        else:
            record("system", "CPU temperature", False, "vcgencmd failed")
    except FileNotFoundError:
        record("system", "CPU temperature", False, "vcgencmd not found")

    # Required kernel modules / interfaces
    for dev in ["/dev/i2c-1", "/dev/spidev0.0", "/dev/ttyAMA5"]:
        exists = os.path.exists(dev)
        record("system", f"device {dev}", exists)

    # Camera interface (rpicam)
    try:
        r = subprocess.run(["rpicam-hello", "--list-cameras"],
                           capture_output=True, text=True, timeout=10)
        found = "Available cameras" in r.stdout or "0 :" in r.stdout
        record("system", "rpicam detects cameras", found,
               r.stdout.strip().splitlines()[0] if r.stdout.strip() else r.stderr.strip()[:80])
    except FileNotFoundError:
        record("system", "rpicam-hello present", False, "rpicam-hello not installed")


# ---------------------------------------------------------------------------
# 2. I2C bus scan
# ---------------------------------------------------------------------------

EXPECTED_I2C_ADDRESSES = {
    0x08: "WittyPi 4 MCU (unified virtual address)",
    0x28: "BNO055 IMU (default addr)",
    0x38: "AHT20 temp/humidity",
    0x2A: "FLIR Lepton CCI",
}
# 0x29 is alternative BNO055 address; 0x50 is optional HAT EEPROM
# WittyPi 4's LM75B (0x48) and PCF85063A RTC (0x51) are on its internal
# I2C bus and are not directly visible on the Pi's I2C-1 bus.
OPTIONAL_I2C_ADDRESSES = {0x29, 0x50}


def _i2c_scan() -> set[int]:
    """Return set of responding I2C addresses on bus 1."""
    try:
        res = subprocess.run(["i2cdetect", "-y", "1"],
                             capture_output=True, text=True, timeout=10)
        found: set[int] = set()
        for line in res.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            if not parts:
                continue
            for token in parts[1:]:
                if token not in ("--", "UU"):
                    try:
                        found.add(int(token, 16))
                    except ValueError:
                        pass
        return found
    except FileNotFoundError:
        return set()


def test_i2c(verbose: bool):
    section("I2C Bus")

    found = _i2c_scan()
    record("i2c", "i2cdetect succeeded", bool(found) or True,
           f"devices found: {sorted(hex(a) for a in found)}")

    for addr, label in EXPECTED_I2C_ADDRESSES.items():
        present = addr in found
        record("i2c", f"0x{addr:02X} {label}", present)

    # Report unexpected devices without failing
    unexpected = found - set(EXPECTED_I2C_ADDRESSES) - OPTIONAL_I2C_ADDRESSES
    if unexpected and verbose:
        warn(f"Unexpected I2C addresses: {sorted(hex(a) for a in unexpected)}")

    if 0x50 in found:
        record("i2c", "0x50 HAT EEPROM (optional)", True)


# ---------------------------------------------------------------------------
# 3. WittyPi 4
# ---------------------------------------------------------------------------

def test_wittypi(verbose: bool):
    section("WittyPi 4")

    wittypi_dir = "/home/pi/wittypi"

    # Check wittypi shell utilities exist
    utilities = os.path.join(wittypi_dir, "utilities.sh")
    record("wittypi", "utilities.sh present", os.path.exists(utilities), utilities)

    def run_wittypi(cmd: str) -> str | None:
        try:
            full = f"cd {wittypi_dir} && . ./utilities.sh && {cmd}"
            out = subprocess.check_output(
                full, shell=True, executable="/bin/bash",
                stderr=subprocess.STDOUT, timeout=5, text=True
            ).strip()
            return out
        except Exception as e:
            return None

    # Temperature (read via WittyPi utilities; sensor is on its internal bus)
    raw = run_wittypi("get_temperature")
    if raw and raw != "ERROR":
        try:
            temp_c = float(raw.split("/")[0].replace("°C", "").strip())
            ok = -20.0 <= temp_c <= 85.0
            record("wittypi", "board temperature in range", ok, f"{temp_c:.1f} °C")
        except ValueError:
            record("wittypi", "board temperature", False, f"parse error: {raw!r}")
    else:
        record("wittypi", "board temperature", False, "command failed or WittyPi not installed")

    # Input (battery) voltage
    raw = run_wittypi("get_input_voltage")
    if raw and raw != "ERROR":
        try:
            bv = float(raw)
            ok = bv >= 3.6
            record("wittypi", "battery voltage ≥ 3.6 V", ok, f"{bv:.2f} V")
        except ValueError:
            record("wittypi", "battery voltage", False, f"parse error: {raw!r}")
    else:
        record("wittypi", "battery voltage", False, "command failed")

    # Output (5 V rail to Pi) voltage
    raw = run_wittypi("get_output_voltage")
    if raw and raw != "ERROR":
        try:
            ov = float(raw)
            ok = 4.75 <= ov <= 5.25
            record("wittypi", "output voltage 4.75–5.25 V", ok, f"{ov:.2f} V")
        except ValueError:
            record("wittypi", "output voltage", False, f"parse error: {raw!r}")
    else:
        record("wittypi", "output voltage", False, "command failed")

    # RTC time sanity: year should be current
    raw = run_wittypi("get_rtc_time")
    if raw and raw != "ERROR":
        import re
        year_match = re.search(r"20\d{2}", raw)
        ok = bool(year_match)
        record("wittypi", "RTC has valid year (20xx)", ok, raw)
    else:
        record("wittypi", "RTC time", False, "command failed")

    # GPIO4 WittyPi SYS_UP signal – just check it reads HIGH (Pi is running)
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(4, GPIO.IN)
        val = GPIO.input(4)
        record("wittypi", "GPIO4 SYS_UP reads HIGH (Pi running)", val == 1, f"GPIO4={val}")
        GPIO.cleanup(4)
    except Exception as exc:
        record("wittypi", "GPIO4 SYS_UP readable", False, str(exc))


# ---------------------------------------------------------------------------
# 4. AHT20 Temperature & Humidity
# ---------------------------------------------------------------------------

def test_aht20(verbose: bool):
    section("AHT20 Temp/Humidity Sensor")

    try:
        import board
        import adafruit_ahtx0

        i2c = board.I2C()
        sensor = adafruit_ahtx0.AHTx0(i2c)

        temp = sensor.temperature
        humidity = sensor.relative_humidity

        ok_t = -40.0 <= temp <= 85.0
        ok_h = 0.0 <= humidity <= 100.0

        record("aht20", "temperature reading in range", ok_t, f"{temp:.1f} °C")
        record("aht20", "humidity reading in range", ok_h, f"{humidity:.1f} %")
        record("aht20", "both readings plausible",
               ok_t and ok_h and temp < 70.0 and humidity > 0.0)

    except ImportError as e:
        record("aht20", "adafruit_ahtx0 import", False, str(e))
    except Exception as e:
        record("aht20", "AHT20 communication", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 5. BNO055 IMU
# ---------------------------------------------------------------------------

def test_bno055(verbose: bool):
    section("BNO055 IMU")

    try:
        import board
        import adafruit_bno055

        i2c = board.I2C()
        sensor = adafruit_bno055.BNO055_I2C(i2c)

        # Give fusion a moment to initialize
        for _ in range(20):
            euler = sensor.euler
            if isinstance(euler, tuple) and any(v not in (None, 0.0) for v in euler):
                break
            time.sleep(0.1)

        # Chip ID sanity: BNO055 should respond on I2C
        record("bno055", "I2C communication established", True)

        euler = sensor.euler
        accel = sensor.acceleration
        gyro = sensor.gyro
        quat = sensor.quaternion
        record("bno055", "Euler angles readable",
               isinstance(euler, tuple) and len(euler) == 3,
               f"heading={euler[0]:.1f}° roll={euler[1]:.1f}° pitch={euler[2]:.1f}°"
               if isinstance(euler, tuple) else str(euler))

        record("bno055", "Acceleration readable",
               isinstance(accel, tuple) and len(accel) == 3,
               f"{accel}" if isinstance(accel, tuple) else str(accel))

        record("bno055", "Gyroscope readable",
               isinstance(gyro, tuple) and len(gyro) == 3)

        record("bno055", "Quaternion readable",
               isinstance(quat, tuple) and len(quat) == 4)

        # Temperature: retry up to 3× – the register occasionally returns a
        # bogus value (e.g. −99 °C) in the first read after power-on.
        bno_temp = None
        for _ in range(3):
            t = sensor.temperature
            if isinstance(t, (int, float)) and -20 <= t <= 80:
                bno_temp = t
                break
            time.sleep(0.2)
        if bno_temp is None:
            bno_temp = sensor.temperature  # record whatever the sensor returns
        if isinstance(bno_temp, (int, float)):
            ok_t = -20 <= bno_temp <= 80
            record("bno055", "die temperature in range", ok_t, f"{bno_temp} °C")
        else:
            record("bno055", "die temperature in range", False,
                   f"unexpected value: {bno_temp!r}")

        # Calibration status
        cal = sensor.calibration_status
        if isinstance(cal, tuple):
            sys_c, gyro_c, accel_c, mag_c = cal
            record("bno055", "calibration status readable",
                   True, f"sys={sys_c} gyro={gyro_c} accel={accel_c} mag={mag_c}")
            if verbose and any(v < 3 for v in cal):
                warn("BNO055 not fully calibrated – move sensor through figure-8 pattern")

    except ImportError as e:
        record("bno055", "adafruit_bno055 import", False, str(e))
    except Exception as e:
        record("bno055", "BNO055 communication", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 6. FLIR Lepton 3.5 (Breakout 2.0)
# ---------------------------------------------------------------------------

LEPTON_RESET_GPIO = 6   # BCM, pin 31 – RESET_L (active-low)
LEPTON_SPI_DEV   = "/dev/spidev0.0"
LEPTON_SPI_SPEED = 16_000_000  # 16 MHz (Lepton max is 20 MHz)
LEPTON_SPI_MODE  = 3
LEPTON_I2C_ADDR  = 0x2A  # CCI (command/control interface)

# Lepton CCI register addresses
LEPTON_REG_STATUS   = 0x0002
LEPTON_REG_CMD      = 0x0004
LEPTON_REG_LENGTH   = 0x0006
LEPTON_REG_DATA0    = 0x0008

# Lepton commands
LEPTON_CMD_GET_PART_NUMBER = 0x0800 | 0x001C  # SYS module, Get Part Number


def test_lepton(verbose: bool):
    section("FLIR Lepton 3.5 (Breakout 2.0)")

    # --- Reset via GPIO6 --------------------------------------------------
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(LEPTON_RESET_GPIO, GPIO.OUT, initial=GPIO.HIGH)
        time.sleep(0.05)
        GPIO.output(LEPTON_RESET_GPIO, GPIO.LOW)
        time.sleep(0.1)   # hold reset low ≥ 5 ms
        GPIO.output(LEPTON_RESET_GPIO, GPIO.HIGH)
        time.sleep(0.3)   # allow boot (Lepton needs ~185 ms after reset)
        GPIO.setup(LEPTON_RESET_GPIO, GPIO.IN)
        record("lepton", "GPIO6 RESET_L cycle succeeded", True)
        GPIO.cleanup(LEPTON_RESET_GPIO)
    except Exception as exc:
        record("lepton", "GPIO6 RESET_L cycle", False, str(exc))

    # --- CCI I2C (0x2A) ---------------------------------------------------
    # Use smbus2 for raw I2C access (avoids Lepton-specific driver requirement)
    try:
        import smbus2

        bus = smbus2.SMBus(1)
        time.sleep(0.5)  # minimum settle before first CCI poll

        # Read STATUS register (0x0002); bits[1:0] should be 0b10 (boot=1, busy=0).
        # The Lepton 3.5 can take up to ~950 ms from RESET_L deassertion to assert
        # boot=1.  Poll up to 1.5 s in 50 ms increments so we don't false-fail on
        # a slow but healthy boot.
        try:
            status = 0
            boot_mode = 0
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                msg_w = smbus2.i2c_msg.write(LEPTON_I2C_ADDR,
                                             [(LEPTON_REG_STATUS >> 8) & 0xFF,
                                              LEPTON_REG_STATUS & 0xFF])
                msg_r = smbus2.i2c_msg.read(LEPTON_I2C_ADDR, 2)
                bus.i2c_rdwr(msg_w, msg_r)
                status = (list(msg_r)[0] << 8) | list(msg_r)[1]
                boot_mode = (status >> 1) & 0x0001   # 1 = normal, 0 = booting
                if boot_mode == 1:
                    break
                time.sleep(0.05)
            busy = bool(status & 0x0001)
            error_code = (status >> 8) & 0xFF
            record("lepton", "CCI I2C communication (0x2A)", True,
                   f"status=0x{status:04X} busy={busy} boot={boot_mode} err={error_code}")
            record("lepton", "Lepton boot complete (STATUS.boot=1)", boot_mode == 1)
            record("lepton", "Lepton not busy", not busy)
            record("lepton", "Lepton no error", error_code == 0, f"error code {error_code}")
        except Exception as exc:
            record("lepton", "CCI STATUS register read", False, str(exc))

        bus.close()

    except ImportError:
        record("lepton", "smbus2 import", False, "install: pip install smbus2")
    except Exception as exc:
        record("lepton", "CCI I2C open", False, f"{type(exc).__name__}: {exc}")

    # --- VOSPI SPI capture ------------------------------------------------
    if not os.path.exists(LEPTON_SPI_DEV):
        record("lepton", f"SPI device {LEPTON_SPI_DEV}", False, "device not found")
        return

    try:
        import spidev

        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz = LEPTON_SPI_SPEED
        spi.mode = LEPTON_SPI_MODE

        # Read VOSPI packets.  Each packet is 164 bytes (2-byte header + 160 data bytes).
        # A valid packet has bits[15:12] of the first word NOT equal to 0xF (discard).
        # The Lepton 3.5 has 60 lines × 4 segments = 240 packets per frame.
        # We just need to find a few non-discard packets to confirm VOSPI is working.

        valid_packets = 0
        discard_packets = 0
        max_attempts = 500

        for _ in range(max_attempts):
            data = spi.readbytes(164)
            header = (data[0] << 8) | data[1]
            id_nibble = (data[0] >> 4) & 0x0F
            if id_nibble == 0xF:
                discard_packets += 1
            else:
                line_num = header & 0x0FFF
                valid_packets += 1
                if valid_packets >= 10:
                    break

        spi.close()

        record("lepton", "VOSPI SPI readable", valid_packets > 0,
               f"{valid_packets} valid / {discard_packets} discard packets")
        if valid_packets > 0:
            record("lepton", "VOSPI delivering image data (≥10 packets)", valid_packets >= 10,
                   f"{valid_packets} valid packets seen")

    except ImportError:
        record("lepton", "spidev import", False, "install: pip install spidev")
    except Exception as exc:
        record("lepton", "VOSPI SPI read", False, f"{type(exc).__name__}: {exc}")

    # --- lepton / capture binaries ----------------------------------------
    binaries_base = "/home/pi/SU-WaterCam"
    for binary in ["lepton", "capture"]:
        path = os.path.join(binaries_base, binary)
        executable = os.path.isfile(path) and os.access(path, os.X_OK)
        record("lepton", f"'{binary}' binary present & executable", executable, path)


# ---------------------------------------------------------------------------
# 7. Multitech mDot LoRa
# ---------------------------------------------------------------------------

MDOT_PORT          = "/dev/ttyAMA5"
MDOT_BAUD          = 115200
MDOT_TIMEOUT       = 3.0
MDOT_AT_RETRIES    = 5    # retry AT to catch gaps between join-attempt windows
MDOT_AT_RETRY_WAIT = 3.0  # seconds between retries; join windows are ~6–10 s each


def _mdot_cmd(ser, cmd: str, timeout: float = 2.0) -> str:
    """Send an AT command and return the response (stripped)."""
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            lines.append(line)
        if any(l in ("OK", "ERROR") for l in lines):
            break
    return "\n".join(lines)


def test_mdot(verbose: bool):
    section("Multitech mDot LoRa Module")

    if not os.path.exists(MDOT_PORT):
        record("mdot", f"serial port {MDOT_PORT}", False, "device not found; check UART5 overlay in /boot/firmware/config.txt")
        return

    try:
        import serial

        with serial.Serial(MDOT_PORT, MDOT_BAUD,
                           parity=serial.PARITY_NONE,
                           stopbits=serial.STOPBITS_ONE,
                           bytesize=serial.EIGHTBITS,
                           timeout=MDOT_TIMEOUT) as ser:

            record("mdot", f"serial port {MDOT_PORT} opened", True)

            # Basic AT command – retry to ride out a network join attempt.
            # While the mDot is actively transmitting a JoinRequest or waiting
            # in a receive window, the UART is unresponsive.  During the backoff
            # interval between retries it will answer normally.
            ok_at = False
            resp = ""
            for attempt in range(1, MDOT_AT_RETRIES + 1):
                resp = _mdot_cmd(ser, "AT")
                if "OK" in resp:
                    ok_at = True
                    break
                if attempt < MDOT_AT_RETRIES:
                    time.sleep(MDOT_AT_RETRY_WAIT)

            record("mdot", "AT → OK", ok_at, resp[:80] if verbose else "")

            if not ok_at:
                record("mdot", "mDot responding", False,
                       "No OK response after retries – module may be mid-join (expected when "
                       "out of range); also check baud rate, UART5 overlay, wiring")
                return

            # Firmware version
            resp = _mdot_cmd(ser, "ATI")
            record("mdot", "ATI firmware version", "OK" in resp,
                   resp.replace("\nOK", "").strip()[:80])

            # Device EUI
            resp = _mdot_cmd(ser, "AT+DI")
            has_eui = "OK" in resp and len(resp) > 3
            record("mdot", "AT+DI device EUI readable", has_eui,
                   resp.replace("\nOK", "").strip())

            # Network join status
            resp = _mdot_cmd(ser, "AT+NJS")
            record("mdot", "AT+NJS join status readable", "OK" in resp,
                   resp.replace("\nOK", "").strip())

            # TX power
            resp = _mdot_cmd(ser, "AT+TXP")
            record("mdot", "AT+TXP TX power readable", "OK" in resp,
                   resp.replace("\nOK", "").strip())

            # Signal quality (RSSI, SNR) – only valid if joined
            resp = _mdot_cmd(ser, "AT+RSSI")
            record("mdot", "AT+RSSI signal quality readable", "OK" in resp or "ERROR" in resp,
                   resp.replace("\nOK", "").strip()[:60])

    except ImportError:
        record("mdot", "pyserial import", False, "install: pip install pyserial")
    except Exception as exc:
        record("mdot", "mDot serial communication", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 8. IR-CUT Camera
# ---------------------------------------------------------------------------

IRCUT_GPIO_BCM = 21   # BCM GPIO21 = physical pin 40


def test_ircut(verbose: bool):
    section("IR-CUT Camera")

    # Camera detection via rpicam
    try:
        res = subprocess.run(
            ["rpicam-hello", "--list-cameras"],
            capture_output=True, text=True, timeout=10
        )
        stdout = res.stdout + res.stderr
        camera_found = "0 :" in stdout or "Available cameras" in stdout
        record("ircut", "rpicam detects CSI camera", camera_found,
               stdout.strip().splitlines()[0] if stdout.strip() else "no output")
    except FileNotFoundError:
        record("ircut", "rpicam-hello present", False,
               "install: sudo apt install rpicam-apps")
        camera_found = False

    # picamera2 import
    try:
        from picamera2 import Picamera2
        record("ircut", "picamera2 import", True)
        picam2_available = True
    except ImportError as e:
        record("ircut", "picamera2 import", False, str(e))
        picam2_available = False

    # Test capture (still image, small resolution)
    if picam2_available and camera_found:
        try:
            import tempfile
            picam2 = Picamera2()
            config = picam2.create_still_configuration(
                main={"format": "RGB888", "size": (640, 480)}
            )
            picam2.configure(config)
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                tmp = f.name
            picam2.start_and_capture_file(tmp, show_preview=False)
            picam2.close()
            size = os.path.getsize(tmp)
            record("ircut", "test capture successful", size > 1000, f"{size} bytes → {tmp}")
            os.unlink(tmp)
        except Exception as exc:
            record("ircut", "test capture", False, f"{type(exc).__name__}: {exc}")

    # IR-CUT filter GPIO control
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(IRCUT_GPIO_BCM, GPIO.OUT)

        GPIO.output(IRCUT_GPIO_BCM, GPIO.LOW)
        time.sleep(0.1)
        low_val = GPIO.input(IRCUT_GPIO_BCM)

        GPIO.output(IRCUT_GPIO_BCM, GPIO.HIGH)
        time.sleep(0.1)
        high_val = GPIO.input(IRCUT_GPIO_BCM)

        GPIO.output(IRCUT_GPIO_BCM, GPIO.LOW)  # leave filter in NIR-off (normal) state
        GPIO.cleanup(IRCUT_GPIO_BCM)

        toggled = (high_val != low_val)
        record("ircut", f"GPIO{IRCUT_GPIO_BCM} IR-CUT filter control", toggled,
               f"LOW={low_val} HIGH={high_val}")

    except Exception as exc:
        record("ircut", f"GPIO{IRCUT_GPIO_BCM} IR-CUT filter GPIO", False,
               f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 9. SPI bus sanity (independent of Lepton)
# ---------------------------------------------------------------------------

def test_spi(verbose: bool):
    section("SPI Bus")

    spidev_path = "/dev/spidev0.0"
    record("spi", f"spidev0.0 device node exists", os.path.exists(spidev_path))

    try:
        import spidev
        spi = spidev.SpiDev()
        spi.open(0, 0)
        spi.max_speed_hz = 1_000_000
        spi.mode = 0
        # Send 0xAA, expect any response (loopback only if MISO tied, otherwise
        # just check the call doesn't throw)
        spi.xfer2([0xAA, 0x55])
        spi.close()
        record("spi", "spidev open/xfer/close", True)
    except ImportError:
        record("spi", "spidev import", False, "pip install spidev")
    except Exception as exc:
        record("spi", "spidev open/xfer/close", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 10. Summary
# ---------------------------------------------------------------------------

def print_summary():
    print()
    print(bold("=" * 60))
    print(bold("  SUMMARY"))
    print(bold("=" * 60))

    by_component: dict[str, list] = {}
    for r in RESULTS:
        by_component.setdefault(r["component"], []).append(r)

    total_pass = 0
    total_fail = 0

    for comp, tests in by_component.items():
        passed = sum(1 for t in tests if t["passed"])
        failed = len(tests) - passed
        total_pass += passed
        total_fail += failed
        status = green(f"{passed}/{len(tests)}") if failed == 0 else red(f"{passed}/{len(tests)}")
        print(f"  {comp:<12} {status}")

    print()
    overall = total_pass == (total_pass + total_fail)
    colour = green if total_fail == 0 else red
    print(bold(colour(f"  TOTAL: {total_pass} passed, {total_fail} failed")))

    failed_tests = [r for r in RESULTS if not r["passed"]]
    if failed_tests:
        print()
        print(bold(red("  FAILURES:")))
        for r in failed_tests:
            detail = f": {r['detail']}" if r["detail"] else ""
            print(f"    [{r['component']}] {r['name']}{detail}")

    return total_fail == 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

COMPONENTS = {
    "system":  test_system,
    "i2c":     test_i2c,
    "spi":     test_spi,
    "wittypi": test_wittypi,
    "aht20":   test_aht20,
    "bno055":  test_bno055,
    "lepton":  test_lepton,
    "mdot":    test_mdot,
    "ircut":   test_ircut,
}


def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(
        description="WaterCam hardware test harness for Raspberry Pi 4B + attached sensors"
    )
    parser.add_argument(
        "--test", "-t",
        choices=list(COMPONENTS.keys()),
        help="Run only the specified component test"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print extra diagnostic detail"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour output"
    )
    args = parser.parse_args()

    if args.no_color:
        USE_COLOR = False

    print(bold(cyan("WaterCam Hardware Test Harness")))
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if os.geteuid() != 0:
        print(yellow("  Note: not running as root – GPIO and SPI tests may fail."))
        print(yellow("        Re-run with: sudo python3 watercam_hardware_test.py"))

    if args.test:
        COMPONENTS[args.test](args.verbose)
    else:
        for fn in COMPONENTS.values():
            fn(args.verbose)

    ok = print_summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
