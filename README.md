# Intruder Alert — Raspberry Pi 5 (HC-SR04 + Camera Module 3)

Short-range intruder detection with optional photo capture and Discord alerts.

## Hardware
- Raspberry Pi 5
- HC-SR04 ultrasonic sensor (VCC=3.3V, TRIG=BCM23, ECHO=BCM24, GND)
- Camera Module 3 + 22↔15 ribbon

## Quick start
```bash
sudo apt update && sudo apt install -y python3-gpiozero python3-requests fswebcam libcamera-apps
cp .env.example .env  # edit to add your real WEBHOOK_URL
python3 src/intruder_alert.py
