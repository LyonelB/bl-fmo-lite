# BL-FMO-LITE

**FM monitoring system for hardware tuners — TEF6686 & Raspiaudio SI4689.**

BL-FMO-LITE is a lightweight FM monitoring application designed for hardware FM tuner chips that decode stereo and RDS internally. Unlike [BL-FMO](https://github.com/LyonelB/fm-monitor), it has no SDR dependency, no MPX spectrum, and no GNU Radio. It runs on a Raspberry Pi with a compatible HAT or dongle.

Part of the **BL ecosystem** — [BL-FMO](https://github.com/LyonelB/fm-monitor) · BL-DMO · BL-FMO-LITE

---

## Features

- FM monitoring via hardware tuner (SI4689 or TEF6686)
- RF quality dashboard: SNR, Multipath, Frequency offset, Stereo/Mono
- RDS decoding: PS, RT, PI, TA/TP flags
- Icecast2 audio streaming via I2S → ffmpeg
- Email alerts on signal loss
- Recording with size limit
- Gunicorn/Flask web interface

## Supported hardware

| Backend | Hardware | Status |
|---|---|---|
| `si4689` | Raspiaudio Digital Radio HAT | ✅ Implemented |
| `tef6686` | TEF6686 Headless Lite Se | 🔜 Incoming |

## Quick start

```bash
git clone https://github.com/LyonelB/bl-fmo-lite.git
cd bl-fmo-lite
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.json.example config.json
# Edit config.json — set tuner.type, frequency, email, etc.
python app.py
```

Open `http://<raspberry-pi-ip>:5000`

## SI4689 (Raspiaudio HAT) prerequisites

The SI4689 backend communicates with the `radio.py` HTTP API from the Raspiaudio project. Start it before BL-FMO-LITE:

```bash
cd /path/to/Digital-Radio-for-Raspberry-Pi
python radio.py serve --port 8686
```

Enable I2S capture in `/boot/firmware/config.txt`:

```
dtparam=i2s=on
dtoverlay=adau7002-simple,card-name=si4689_i2s
```

Then reboot and verify:

```bash
arecord -l  # should show si4689_i2s
```

## systemd service

```bash
sudo cp bl-fmo-lite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bl-fmo-lite
sudo systemctl start bl-fmo-lite
```

## Configuration

See `config.json.example` and `CONFIGURATION.md`.

Key section:
```json
"tuner": {
    "type": "si4689",
    "frequency": "88.6M",
    "api_url": "http://localhost:8686",
    "alsa_device": "plughw:CARD=si4689_i2s,DEV=0"
}
```

## BL ecosystem naming convention

`BL-[band][function]` — BL = Bernard Lyonel, band = FM/D(AB+), function = MO(nitor)/TU(ner).

---

MIT License — Graffiti Radio, La Roche-sur-Yon
