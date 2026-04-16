#!/usr/bin/env python3
"""
IR-CUT filter movement test.

Drives GPIO21 (pin 40) through a LOW → HIGH → LOW sequence, captures an image
in each state with camera controls locked, and checks whether the scene changes
in a way consistent with the IR-CUT filter physically moving.

How the test works
------------------
The IR-CUT filter blocks near-infrared light when in place (day mode).
Removing it lets IR through, dramatically increasing how much light reaches
the sensor.  With the camera controls locked (fixed exposure and gain), this
appears as a large increase in overall image brightness — typically 30–50 %
under indoor lighting.  Brightness is used as the primary metric.

R/B channel ratio is NOT used as the primary metric for this sensor.  The
OV5647's blue pixels are more IR-sensitive than red, so removing the filter
increases the blue channel more than red.  R/B therefore moves in the wrong
direction and by a small amount, making it an unreliable discriminator.
R/G ratio (reported as a diagnostic) is more useful if a ratio is needed:
the green channel is less IR-sensitive than red, so R/G rises when IR
increases.

Why camera controls are locked before capturing
-----------------------------------------------
The camera's Auto White Balance (AWB) and Auto Exposure (AEC) compensate for
exactly the kind of color and brightness change the IR-CUT filter produces.
Given enough time to settle, AWB will equalise the channel ratios and AEC will
equalise brightness, masking the filter movement entirely.

To prevent this, we let AWB and AEC settle during warm-up, then snapshot the
current gains and exposure time and lock them before any GPIO change.  Both
captures then happen under identical conditions, so any R/B shift must come
from the filter, not the camera.

Why we test the return transition
----------------------------------
A single HIGH-vs-LOW comparison can produce a false positive if scene lighting
changes between captures (e.g. a cloud passing).  We drive back to LOW after
the HIGH capture and check that the R/B ratio returns toward its original value.
A real filter movement is symmetric; an incidental lighting change is not.

Settle time
-----------
With camera controls locked, we only need to wait long enough for the filter
to physically move (~300 ms) — not for AWB to stabilise.  FILTER_SETTLE_S is
set to 0.5 s.

Usage:
    sudo python3 test_ircut_filter.py
    sudo python3 test_ircut_filter.py --save-images   # writes PNG files
    sudo python3 test_ircut_filter.py --gpio 22       # override GPIO pin (BCM)
    sudo python3 test_ircut_filter.py --threshold 0.05

Lighting note:
    The test requires a scene with meaningful near-IR content.  Sunlight and
    halogen lamps work well.  Pure LED panels have very little IR; under LED
    lighting the R/B shift is small even when the filter moves correctly.  Use
    --save-images for visual confirmation when LED-only lighting is unavoidable.

Requires: picamera2, numpy, RPi.GPIO
    pip install picamera2 numpy RPi.GPIO
"""

import argparse
import sys
import time

import numpy as np

try:
    import RPi.GPIO as GPIO
except ImportError:
    sys.exit("RPi.GPIO not found.  Install with: pip install RPi.GPIO")

try:
    from picamera2 import Picamera2
except ImportError:
    sys.exit("picamera2 not found.  Install with: pip install picamera2")

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

IRCUT_GPIO_BCM  = 21          # BCM GPIO21 = physical pin 40
CAPTURE_SIZE    = (640, 480)  # small enough to be fast
WARMUP_S        = 3.0         # AWB/AEC settling time before locking
LOCK_SETTLE_S   = 0.3         # pause after applying locked controls
FILTER_SETTLE_S = 0.5         # after GPIO change; filter moves in ~300 ms
FRAMES_AVERAGED = 5           # frames averaged per state to reduce noise
FRAME_INTERVAL  = 0.1         # seconds between averaged frames

# Primary pass threshold: relative brightness change between LOW and HIGH.
# With camera controls locked, brightness directly reflects sensor light input.
# Observed values: ~40–50 % under indoor lighting when filter moves.
# Threshold is set conservatively below that to allow for dimmer scenes.
DEFAULT_THRESHOLD = 0.20      # 20 % relative brightness change


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mean_rgb(picam2: "Picamera2", n: int, interval: float) -> np.ndarray:
    """Return per-channel mean (R, G, B) averaged over n frames."""
    accumulator = None
    for i in range(n):
        if i:
            time.sleep(interval)
        frame = picam2.capture_array()       # (H, W, 3) uint8
        arr = frame.astype(np.float32)
        accumulator = arr if accumulator is None else accumulator + arr
    return (accumulator / n).mean(axis=(0, 1))   # (R, G, B)


def _rg_ratio(rgb: np.ndarray) -> float:
    """R/G ratio — rises when IR increases (G is less IR-sensitive than R)."""
    r, g, _ = rgb
    return float(r / g) if g > 0 else float("inf")


def _lock_camera(picam2: "Picamera2") -> dict:
    """
    Snapshot current AWB/AEC state and lock it so both filter-state captures
    happen under identical conditions.  Returns the locked values for logging.
    """
    meta   = picam2.capture_metadata()
    gains  = meta.get("ColourGains", (1.0, 1.0))
    exp    = meta.get("ExposureTime", 10_000)
    gain   = meta.get("AnalogueGain", 1.0)
    picam2.set_controls({
        "AwbEnable":    False,
        "ColourGains":  gains,
        "AeEnable":     False,
        "ExposureTime": exp,
        "AnalogueGain": gain,
    })
    time.sleep(LOCK_SETTLE_S)
    return {"ColourGains": gains, "ExposureTime": exp, "AnalogueGain": gain}


def _save_image(picam2: "Picamera2", label: str, gpio_pin: int):
    try:
        from PIL import Image  # type: ignore
        frame = picam2.capture_array()
        fname = f"ircut_gpio{gpio_pin}_{label}.png"
        Image.fromarray(frame).save(fname)
        print(f"    Saved {fname}")
    except ImportError:
        print("    (Pillow not installed — skipping image save)")


def _capture_state(picam2, label, gpio_pin, level, save_images):
    GPIO.output(gpio_pin, level)
    print(f"\n  GPIO {'HIGH' if level == GPIO.HIGH else 'LOW'} [{label}]"
          f" — settling {FILTER_SETTLE_S:.1f} s …", end="", flush=True)
    time.sleep(FILTER_SETTLE_S)
    print(" capturing …", end="", flush=True)

    rgb        = _mean_rgb(picam2, FRAMES_AVERAGED, FRAME_INTERVAL)
    brightness = float(rgb.mean())
    rg         = _rg_ratio(rgb)

    print(" done")
    print(f"    R={rgb[0]:.1f}  G={rgb[1]:.1f}  B={rgb[2]:.1f}  "
          f"brightness={brightness:.1f}  R/G={rg:.3f}")

    if save_images:
        _save_image(picam2, label, gpio_pin)

    return {"rgb": rgb, "brightness": brightness, "rg": rg}


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_test(gpio_pin: int, threshold: float, save_images: bool) -> bool:
    print(f"IR-CUT filter movement test  (GPIO{gpio_pin} BCM / pin 40)")
    print("-" * 60)

    # --- GPIO: start in LOW (day mode) so first transition is known ----------
    GPIO.setwarnings(False)   # suppress "channel already in use" on re-runs
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(gpio_pin, GPIO.OUT)
    GPIO.output(gpio_pin, GPIO.LOW)

    # --- Camera setup --------------------------------------------------------
    picam2 = Picamera2()
    config = picam2.create_preview_configuration(
        main={"format": "RGB888", "size": CAPTURE_SIZE}
    )
    picam2.configure(config)
    picam2.start()
    print(f"Camera started — warming up {WARMUP_S:.0f} s (AWB/AEC settling) …")
    time.sleep(WARMUP_S)

    # --- Lock AWB and AEC ----------------------------------------------------
    lock = _lock_camera(picam2)
    print(f"Camera locked — gains={lock['ColourGains']}  "
          f"exposure={lock['ExposureTime']} µs  "
          f"gain={lock['AnalogueGain']:.2f}")

    # --- Capture sequence: LOW → HIGH → LOW ----------------------------------
    low_base   = _capture_state(picam2, "low_base",   gpio_pin, GPIO.LOW,  save_images)
    high       = _capture_state(picam2, "high",       gpio_pin, GPIO.HIGH, save_images)
    low_return = _capture_state(picam2, "low_return", gpio_pin, GPIO.LOW,  save_images)

    # --- Cleanup -------------------------------------------------------------
    picam2.stop()
    picam2.close()
    GPIO.output(gpio_pin, GPIO.LOW)   # leave filter in day mode
    GPIO.cleanup(gpio_pin)

    # --- Evaluate ------------------------------------------------------------
    print()
    print("-" * 60)

    b_low  = low_base["brightness"]
    b_high = high["brightness"]
    b_ret  = low_return["brightness"]

    # Primary: did brightness rise significantly when filter was removed?
    forward_diff = b_high - b_low          # expect positive (more light)
    forward_rel  = forward_diff / b_low if b_low > 0 else 0.0

    # Return: is low_return closer to baseline than to high?
    # With a locked camera this is unambiguous — if the filter returned,
    # brightness should drop back toward the baseline value.
    dist_to_base = abs(b_ret - b_low)
    dist_to_high = abs(b_ret - b_high)
    returned     = dist_to_base < dist_to_high

    # Diagnostic: R/G ratio (rises with IR; reported but not used for pass/fail)
    rg_low  = low_base["rg"]
    rg_high = high["rg"]
    rg_rel  = (rg_high - rg_low) / rg_low if rg_low > 0 else 0.0

    print(f"Brightness  low_base={b_low:.1f}  high={b_high:.1f}  "
          f"low_return={b_ret:.1f}")
    print(f"Forward shift:  {forward_rel*100:.1f} %  (threshold {threshold*100:.0f} %)")
    print(f"Return check:   low_return closer to "
          f"{'baseline ✓' if returned else 'HIGH — filter may not have returned ✗'}")
    print(f"R/G (diagnostic): low_base={rg_low:.3f}  high={rg_high:.3f}  "
          f"shift={rg_rel*100:.1f} %")
    print()

    forward_pass = forward_rel >= threshold
    overall_pass = forward_pass and returned

    if overall_pass:
        print(f"PASS  brightness increased {forward_rel*100:.1f} % and returned — "
              f"IR-CUT filter is moving in both directions")
    elif forward_pass and not returned:
        print(f"PASS (partial)  brightness increased {forward_rel*100:.1f} % but did not "
              f"return on LOW — filter may be stuck in night mode")
    else:
        print(f"FAIL  brightness changed only {forward_rel*100:.1f} % (< {threshold*100:.0f} %)")
        print()
        print("Possible causes:")
        print("  - GPIO not reaching the photoresistor node (check wiring)")
        print("  - Filter stuck or not responding to GPIO voltage swing")
        print("  - Use --save-images to inspect captures visually")

    return overall_pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test IR-CUT filter movement by comparing images across GPIO states"
    )
    parser.add_argument("--gpio", type=int, default=IRCUT_GPIO_BCM,
                        help=f"BCM GPIO number (default: {IRCUT_GPIO_BCM})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Min relative R/B ratio change to pass (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--save-images", action="store_true",
                        help="Save PNG images from each GPIO state for manual inspection")
    args = parser.parse_args()

    ok = run_test(args.gpio, args.threshold, args.save_images)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
