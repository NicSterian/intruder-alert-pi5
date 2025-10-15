#!/usr/bin/env python3  # Tell the shell to run this file with Python 3

"""
HC-SR04 Intruder Alert (Raspberry Pi 5) — safe 3.3V mode
- GPIO backend: prefers lgpio (Pi 5 friendly), auto-fallback to pigpio, then default
- Discord webhook on trigger (one alert per cooldown window)
- Optional photo using rpicam-still (Bookworm), fallback to libcamera-still → fswebcam
- Clear logs for: TRIGGER→send, TRIGGER→cooldown (not sending), and CLEAR (out of range)
"""

from __future__ import annotations  # Forward annotation support on older 3.x

# --- Standard library imports (OS/process/time/logging/utilities) ---
import os               # Read environment variables (WEBHOOK_URL, etc.)
import time             # Sleep intervals and timestamps for cooldown
import sys              # Access stdout for logging stream handler
import logging          # Uniform console logs the examiner can read
import subprocess       # Call CLI camera tools (rpicam-still, fswebcam)
import shutil           # Check presence of CLI tools with shutil.which
from datetime import datetime  # Timestamp in Discord message
from typing import Optional     # Type hint for optional image path

# --- Third-party libs installed via apt/pip ---
from gpiozero import Device, DistanceSensor  # High-level GPIO abstraction
import requests                              # HTTP client for Discord webhook

# -------------------- CONFIG (edit here or use env) --------------------

TRIG_GPIO = 23  # BCM (physical pin 16) – HC-SR04 TRIG pin
ECHO_GPIO = 24  # BCM (physical pin 18) – HC-SR04 ECHO pin

POWERED_AT_3V3 = True                      # We wire VCC of HC-SR04 to 3.3V (safe ECHO to Pi)
MAX_DISTANCE_M = 1.5 if POWERED_AT_3V3 else 3.5  # Conservative max distance depending on supply

# Trigger threshold (cm). Can be overridden at runtime: INTRUDER_THRESHOLD_CM=60 python3 ...
DISTANCE_THRESHOLD_CM = float(os.getenv("INTRUDER_THRESHOLD_CM", "35"))

# Sensor read cadence (seconds). Can be overridden via INTRUDER_SAMPLE_S
SAMPLE_INTERVAL = float(os.getenv("INTRUDER_SAMPLE_S", "0.25"))

# Cooldown duration (seconds) between notifications. Overridable via INTRUDER_COOLDOWN
COOLDOWN_SECONDS = float(os.getenv("INTRUDER_COOLDOWN", "30"))

# Whether to capture and attach a photo. Runtime override: SEND_PHOTO=0 (false/no)
SEND_PHOTO = os.getenv("SEND_PHOTO", "1").lower() not in ("0", "false", "no")

# Where a captured image is written temporarily
PHOTO_PATH = "/tmp/intruder.jpg"

# Discord webhook URL. Prefer passing via env to avoid leaking secrets.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "PUT_A_NEW_DISCORD_WEBHOOK_HERE")

# ----------------------------------------------------------------------

# -------------------- Logging --------------------

logging.basicConfig(                       # Configure global logging once
    level=logging.INFO,                    # INFO is readable in demos; DEBUG would be too chatty
    format="%(asctime)s [%(levelname)s] %(message)s",  # Timestamp + level + message
    handlers=[logging.StreamHandler(sys.stdout)],      # Print to console
)

# -------------------- GPIO backend selection --------------------
# Prefer lgpio on Pi 5 (fast, kernel-backed). If unavailable, try pigpio. Otherwise fall back to default.

def _select_backend() -> None:
    try:
        from gpiozero.pins.lgpio import LGPIOFactory  # Import inside try so we can fall back cleanly
        Device.pin_factory = LGPIOFactory()           # Set lgpio as the active backend
        logging.info("GPIO backend: lgpio")
        return
    except Exception as e:
        logging.warning(f"lgpio unavailable ({e}); trying pigpio...")

    try:
        from gpiozero.pins.pigpio import PiGPIOFactory  # Second choice backend
        Device.pin_factory = PiGPIOFactory()            # Requires pigpiod (daemon); OK if not present
        logging.info("GPIO backend: pigpio")
        return
    except Exception as e:
        logging.warning(f"pigpio unavailable ({e}); using gpiozero default backend.")

_select_backend()  # Decide the backend once at startup

# -------------------- Camera helpers --------------------

def _run_quiet(cmd: list[str]) -> bool:
    """Run a CLI command and return True on success, suppressing stdout/stderr for clean logs."""
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def capture_image(path: str) -> bool:
    """
    Take a snapshot to 'path'.
    Preference order:
    1) rpicam-still (Bookworm default)
    2) libcamera-still (legacy name)
    3) fswebcam (USB webcam fallback)
    Return True iff the output file exists afterwards.
    """
    try:
        if os.path.exists(path):   # Remove stale file so existence check is meaningful
            os.remove(path)
    except Exception:
        pass

    # Primary: rpicam-still on Bookworm (Camera Module 3 friendly)
    if shutil.which("rpicam-still"):
        if _run_quiet([
            "rpicam-still", "-n",           # -n: no preview (headless)
            "--zsl",                        # Zero Shutter Lag for better quality
            "-t", "500",                    # Short warm-up (ms)
            "--immediate",                  # Capture now
            "--width", "1280", "--height", "720",  # Reasonable resolution for webhook
            "-o", path                      # Output file path
        ]):
            return os.path.isfile(path)

    # Legacy: libcamera-still may still be present on some systems
    if shutil.which("libcamera-still"):
        if _run_quiet([
            "libcamera-still", "-n",
            "-t", "500", "--immediate",
            "--width", "1280", "--height", "720",
            "-o", path
        ]):
            return os.path.isfile(path)

    # USB fallback: fswebcam if a USB camera is attached
    if shutil.which("fswebcam"):
        if _run_quiet(["fswebcam", "-r", "1280x720", "--no-banner", path]):
            return os.path.isfile(path)

    return False  # None of the commands succeeded

# -------------------- Discord --------------------

def send_discord(distance_cm: float, image_path: Optional[str] = None) -> None:
    """
    Post an alert to Discord. If image_path is valid, attach the photo.
    Uses a simple JSON payload for text-only, or multipart for file upload.
    """
    if not WEBHOOK_URL or "PUT_A_NEW_DISCORD_WEBHOOK_HERE" in WEBHOOK_URL:
        logging.error("No valid WEBHOOK_URL set. Set env WEBHOOK_URL or edit the script.")
        return

    # Human-readable message with distance and time (matches your screenshots)
    content = f":rotating_light: **Intruder detected** — {distance_cm:.1f} cm at {datetime.now():%H:%M:%S}"

    try:
        if image_path and os.path.isfile(image_path):
            # Multipart POST when a file is attached
            with open(image_path, "rb") as f:
                r = requests.post(
                    WEBHOOK_URL,
                    data={"content": content},
                    files={"file": ("intruder.jpg", f, "image/jpeg")},
                    timeout=15,
                )
        else:
            # JSON POST for text-only alert
            r = requests.post(WEBHOOK_URL, json={"content": content}, timeout=15)

        if 200 <= r.status_code < 300:
            logging.info("Discord: sent alert successfully.")
        else:
            logging.warning(f"Discord: failed ({r.status_code}) {r.text[:200]}")
    except Exception as e:
        logging.exception(f"Discord webhook error: {e}")

# -------------------- Main loop --------------------

def main() -> None:
    """Initialise the sensor, then loop: read → check threshold → (optionally) snapshot → send → cooldown."""
    sensor = None  # Keep a handle so we can close() safely in finally
    try:
        # Create DistanceSensor with configured pins and an averaging queue for stability
        sensor = DistanceSensor(
            echo=ECHO_GPIO,
            trigger=TRIG_GPIO,
            max_distance=MAX_DISTANCE_M,
            queue_len=3,          # small smoothing; higher values = steadier but slower
        )

        # gpiozero uses 0..1 for distance. threshold_distance expects meters/max_distance.
        sensor.threshold_distance = DISTANCE_THRESHOLD_CM / 100.0

        # One-time banner so the examiner sees the runtime config immediately
        logging.info(
            f"3.3V mode (max_distance={MAX_DISTANCE_M} m) • "
            f"threshold={DISTANCE_THRESHOLD_CM:.1f} cm • "
            f"cooldown={COOLDOWN_SECONDS:.0f}s • "
            f"photo={'ON' if SEND_PHOTO else 'OFF'}"
        )
        if POWERED_AT_3V3:
            logging.info("HC-SR04 VCC at 3.3V — safe ECHO, shorter range (good for demo).")

        last_sent = 0.0   # Timestamp of last successful send (seconds since epoch)
        was_in_range = False  # Track edge transitions for “CLEAR” logging

        while True:
            # Convert gpiozero's relative reading into centimeters
            dist_m = sensor.distance * sensor.max_distance     # distance in meters
            dist_cm = dist_m * 100.0                           # convert to cm
            in_range = dist_cm <= DISTANCE_THRESHOLD_CM        # trigger condition

            now = time.time()                                  # current timestamp (s)
            cooldown_left = max(0.0, COOLDOWN_SECONDS - (now - last_sent))  # remaining cooldown

            if in_range:
                if cooldown_left <= 0.0:
                    # --- Trigger and not on cooldown: we will send a notification ---
                    logging.info(
                        f"TRIGGER: {dist_cm:.1f} cm → sending Discord alert "
                        f"(cooldown will be {COOLDOWN_SECONDS:.0f}s)."
                    )

                    # Try to capture a photo if enabled; otherwise send text-only
                    img = None
                    if SEND_PHOTO:
                        if capture_image(PHOTO_PATH):
                            img = PHOTO_PATH
                        else:
                            logging.warning("Camera: capture failed; sending text-only.")

                    send_discord(dist_cm, img)      # Post to Discord (with/without image)
                    last_sent = time.time()         # Start cooldown now
                else:
                    # --- Triggered but still cooling down: log it for evidence, don't send ---
                    logging.info(
                        f"TRIGGER: {dist_cm:.1f} cm — on cooldown "
                        f"({cooldown_left:.0f}s left). NOT sending."
                    )
                was_in_range = True                 # Remember we are inside the zone
            else:
                # We only log CLEAR when crossing from in-range → out-of-range to avoid spam
                if was_in_range:
                    logging.info("CLEAR: Out of range.")
                    was_in_range = False

            time.sleep(SAMPLE_INTERVAL)             # Pace the loop to a stable, readable rate

    except KeyboardInterrupt:
        logging.info("Stopped by user.")            # Graceful exit on Ctrl+C
    finally:
        if sensor is not None:
            try:
                sensor.close()                      # Release GPIO resources explicitly
            except Exception:
                logging.exception("Error closing sensor")
        logging.info("GPIO released.")              # Final line confirms cleanup

# -------------------- Entrypoint --------------------

if __name__ == "__main__":  # Only run main() when executed as a script (not when imported)
    main()
