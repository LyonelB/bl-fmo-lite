"""
BL-FMO-LITE — tuners/si4689.py
Backend Raspiaudio SI4689 — radio.py permanent.

radio.py tourne en permanence (gere le SPI/firmware).
BL-FMO-LITE utilise son API HTTP + notre patch RDS direct.
Audio : stream relay depuis /audio/live.mp3.

config.json :
    "tuner": {
        "type":          "si4689",
        "frequency":     "88.6M",
        "radio_py_path": "/home/graffiti/bl-fmo-lite/vendor/raspiaudio/radio.py",
        "api_port":      8686,
        "alsa_device":   "plughw:CARD=si4689i2s,DEV=0"
    }
"""

import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

import requests

from .base import TunerBase

log = logging.getLogger(__name__)

_VENDOR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "vendor", "raspiaudio"
)
_DATA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data"
)


def _load_rds_decoder():
    import sys as _sys
    if _VENDOR not in _sys.path:
        _sys.path.insert(0, _VENDOR)
    from raspiaudio_radio.fm_rds_decoder import FMRDSDecoder
    return FMRDSDecoder


class SI4689Tuner(TunerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.radio_py    = config.get(
            "radio_py_path",
            os.path.join(_VENDOR, "radio.py")
        )
        self.api_port    = int(config.get("api_port", 8686))
        self.api_url     = f"http://localhost:{self.api_port}"

        self._radio_proc: Optional[subprocess.Popen] = None
        self._session    = requests.Session()
        self._session.timeout = 3
        self._rds_decoder = None
        self._last_status: dict = {}
        self._last_rds:    dict = {}

        # Relay stream
        self._relay_listeners: list = []
        import threading
        self._relay_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        os.makedirs(os.path.join(_DATA, "recordings"), exist_ok=True)

        if not self._start_radio_py():
            return False

        if not self._wait_for_api(timeout=15):
            log.error("SI4689: API inaccessible")
            return False

        self._boot_fm()

        if not self.tune(self.frequency_mhz):
            log.warning("SI4689: syntonisation echouee — on continue")

        try:
            Decoder = _load_rds_decoder()
            self._rds_decoder = Decoder()
        except Exception as e:
            log.warning("SI4689: RDS decoder indisponible : %s", e)

        self._running = True
        log.info("SI4689: demarré — %.1f MHz", self.frequency_mhz)

        import threading
        threading.Thread(
            target=self._stream_relay, daemon=True, name="si4689-relay"
        ).start()

        return True

    def stop(self):
        self._running = False
        self._stop_radio_py()
        self._session.close()
        log.info("SI4689: arrete")

    # ------------------------------------------------------------------
    # radio.py subprocess
    # ------------------------------------------------------------------

    def _start_radio_py(self) -> bool:
        if self._radio_proc and self._radio_proc.poll() is None:
            return True
        try:
            subprocess.run(
                ["pkill", "-f", f"radio.py serve --port {self.api_port}"],
                capture_output=True, timeout=3
            )
            time.sleep(0.5)
        except Exception:
            pass
        if not os.path.exists(self.radio_py):
            log.error("SI4689: radio.py introuvable : %s", self.radio_py)
            return False
        try:
            self._radio_proc = subprocess.Popen(
                [sys.executable, self.radio_py, "serve",
                 "--port", str(self.api_port),
                 "--state-file",     os.path.join(_DATA, "runtime_state.json"),
                 "--fm-scan",        os.path.join(_DATA, "scan_fm.json"),
                 "--recordings-dir", os.path.join(_DATA, "recordings")],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            log.info("SI4689: radio.py demarre (PID %d)", self._radio_proc.pid)
            return True
        except Exception as e:
            log.error("SI4689: impossible de lancer radio.py : %s", e)
            return False

    def _stop_radio_py(self):
        if not self._radio_proc:
            return
        if self._radio_proc.poll() is not None:
            self._radio_proc = None
            return
        try:
            os.killpg(os.getpgid(self._radio_proc.pid), signal.SIGTERM)
            self._radio_proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(self._radio_proc.pid), signal.SIGKILL)
            except Exception:
                pass
        finally:
            self._radio_proc = None

    def _ensure_radio_running(self) -> bool:
        if self._radio_proc and self._radio_proc.poll() is None:
            return True
        log.warning("SI4689: radio.py mort — relance")
        if self._start_radio_py():
            return self._wait_for_api(timeout=10)
        return False

    def _wait_for_api(self, timeout: int = 15) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self._session.get(f"{self.api_url}/api/status", timeout=2)
                if r.ok:
                    return True
            except requests.RequestException:
                pass
            time.sleep(1)
        return False

    # ------------------------------------------------------------------
    # Boot FM
    # ------------------------------------------------------------------

    def _boot_fm(self):
        for endpoint, payload in [
            ("/api/boot", {"mode": "fm"}),
            ("/api/mode", {"mode": "fm"}),
        ]:
            try:
                r = self._session.post(
                    f"{self.api_url}{endpoint}", json=payload, timeout=15
                )
                if r.ok:
                    log.info("SI4689: boot FM via %s", endpoint)
                    time.sleep(2)
                    return
            except requests.RequestException:
                continue
        log.warning("SI4689: boot FM echoue — chip peut-etre deja en FM")

    # ------------------------------------------------------------------
    # Tuning
    # ------------------------------------------------------------------

    def tune(self, frequency_mhz: float) -> bool:
        freq_khz   = int(frequency_mhz * 1000)
        station_id = f"fm:{freq_khz}"
        for endpoint, payload in [
            ("/api/play", {"station_id": station_id}),
            ("/api/play", {"frequency": freq_khz * 1000, "mode": "fm"}),
        ]:
            try:
                r = self._session.post(
                    f"{self.api_url}{endpoint}", json=payload, timeout=5
                )
                if r.ok:
                    log.info("SI4689: syntonise %s", station_id)
                    return True
            except requests.RequestException:
                pass
        # Verifier si deja syntonise
        try:
            r = self._session.get(f"{self.api_url}/api/status", timeout=3)
            if r.ok:
                current = (r.json().get("data") or {}).get("current_station") or {}
                if current.get("freq_khz") == freq_khz:
                    log.info("SI4689: deja sur %s", station_id)
                    return True
        except Exception:
            pass
        log.warning("SI4689: syntonisation impossible — peut-etre deja sur %.1f MHz", frequency_mhz)
        return True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        if not self._ensure_radio_running():
            return self._last_status or self._empty_status()
        try:
            r = self._session.get(f"{self.api_url}/api/status", timeout=3)
            if not r.ok:
                return self._last_status or self._empty_status()
            body = r.json()
            data = body.get("data", body)
            status = self._parse_status(data)
            self._last_status = status
            return status
        except requests.RequestException as exc:
            log.debug("SI4689 status error: %s", exc)
            return self._last_status or self._empty_status()

    def _parse_status(self, data: dict) -> dict:
        sig = data.get("signal") or {}
        rssi = self._to_float(sig.get("rssi"))
        signal_ok = rssi is not None and rssi >= 10
        return {
            "signal_dbf":     rssi,
            "snr":            self._to_float(sig.get("snr")),
            "multipath":      None,
            "offset_hz":      (self._to_int(sig.get("freqoff")) or 0) * 10,
            "stereo_present": bool(
                sig.get("blend_control") == 0
                and sig.get("analog_source", False)
            ),
            "signal_ok":      signal_ok,
            "rds_ta":         None,
            "rds_tp":         None,
            "tuner_type":     "si4689",
        }

    def _empty_status(self) -> dict:
        return {
            "signal_dbf": None, "snr": None, "multipath": None,
            "offset_hz": None, "stereo_present": None, "signal_ok": False,
            "rds_ta": None, "rds_tp": None, "tuner_type": "si4689",
        }

    # ------------------------------------------------------------------
    # RDS (direct SPI via patch vendor)
    # ------------------------------------------------------------------

    def get_rds(self) -> dict:
        rds_data = self._get_rds_from_api()
        if rds_data:
            return rds_data
        return self._last_rds

    def _get_rds_from_api(self) -> dict:
        try:
            r = self._session.get(f"{self.api_url}/api/fm/rds", timeout=3)
            if not r.ok:
                return {}
            body = r.json()
            rds  = (body.get("data") or {}).get("rds") or {}
            if any(v for v in rds.values() if v):
                self._last_rds = rds
                return rds
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------------
    # Stream relay multi-clients
    # ------------------------------------------------------------------

    @property
    def stream_url(self) -> str:
        return f"http://localhost:{self.api_port}/audio/live.mp3"

    def _stream_relay(self):
        import requests as _req
        session = _req.Session()
        log.info("SI4689 relay: demarrage")
        while self._running:
            try:
                with session.get(
                    self.stream_url, stream=True, timeout=(10, None)
                ) as r:
                    if not r.ok:
                        log.warning("SI4689 relay: HTTP %d", r.status_code)
                        time.sleep(5)
                        continue
                    log.info("SI4689 relay: stream actif")
                    for chunk in r.iter_content(chunk_size=4096):
                        if not self._running or not chunk:
                            break
                        with self._relay_lock:
                            dead = []
                            for q in self._relay_listeners:
                                try:
                                    q.put_nowait(chunk)
                                except Exception:
                                    dead.append(q)
                            for q in dead:
                                self._relay_listeners.remove(q)
            except Exception as exc:
                log.debug("SI4689 relay: %s", exc)
            if self._running:
                time.sleep(5)
        session.close()

    def stream_listener(self):
        import queue
        q = queue.Queue(maxsize=256)
        with self._relay_lock:
            self._relay_listeners.append(q)
        return q

    def stream_unlisten(self, q):
        with self._relay_lock:
            try:
                self._relay_listeners.remove(q)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_float(v) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(v) -> Optional[int]:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
