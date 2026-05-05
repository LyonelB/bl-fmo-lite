"""
BL-FMO-LITE — tuners/si4689.py
Backend pour le Raspiaudio Digital Radio HAT (Skyworks SI4689).

Autonome : démarre et arrête radio.py serve automatiquement.
L'utilisateur n'a pas besoin de lancer radio.py manuellement.

config.json :
    "tuner": {
        "type": "si4689",
        "frequency": "88.6M",
        "radio_py_path": "/home/graffiti/Digital-Radio-for-Raspberry-Pi/radio.py",
        "api_port": 8686,
        "alsa_device": "plughw:CARD=si4689i2s,DEV=0"
    }
"""

import logging
import subprocess
import sys
import time
import signal
import os
from typing import Optional

import requests

from .base import TunerBase

log = logging.getLogger(__name__)


class SI4689Tuner(TunerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_port    = int(config.get("api_port", 8686))
        self.api_url     = f"http://localhost:{self.api_port}"
        self.radio_py    = config.get(
            "radio_py_path",
            "/home/graffiti/Digital-Radio-for-Raspberry-Pi/radio.py"
        )
        self._radio_proc: Optional[subprocess.Popen] = None
        self._session    = requests.Session()
        self._session.timeout = 3
        self._last_status: dict = {}
        self._last_rds: dict    = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def stream_url(self) -> str:
        """radio.py fournit son propre stream — pas besoin d'Icecast."""
        return f"http://localhost:{self.api_port}/audio/live.mp3"

    def start(self) -> bool:
        # 1. Démarrer radio.py serve
        if not self._start_radio_py():
            return False

        # 2. Attendre que l'API réponde
        if not self._wait_for_api(timeout=15):
            log.error("SI4689: API radio.py inaccessible sur %s", self.api_url)
            return False

        # 3. Booter le chip en mode FM
        self._boot_fm()

        # 4. Syntoniser la fréquence configurée
        ok = self.tune(self.frequency_mhz)
        if ok:
            self._running = True
            log.info("SI4689: démarré — %.1f MHz", self.frequency_mhz)
            # 5. Keepalive : consommer le stream pour activer les métriques RF
            import threading
            threading.Thread(
                target=self._stream_keepalive, daemon=True, name="si4689-keepalive"
            ).start()
        return ok

    def _stream_keepalive(self):
        """
        Consomme /audio/live.mp3 en permanence pour maintenir le chip actif
        et forcer la mise à jour des métriques RF dans /api/status.
        Utilise une session dédiée (requests.Session n'est pas thread-safe).
        """
        import time as _time
        import requests as _req

        ka_session = _req.Session()
        log.info("SI4689 keepalive: démarrage")

        while self._running:
            try:
                with ka_session.get(
                    f"{self.api_url}/audio/live.mp3",
                    stream=True,
                    timeout=(10, None)   # connect=10s, read=pas de timeout
                ) as r:
                    if not r.ok:
                        log.warning("SI4689 keepalive: HTTP %d — relance dans 10s", r.status_code)
                        _time.sleep(10)
                        continue
                    log.info("SI4689 keepalive: stream actif (HTTP %d)", r.status_code)
                    for chunk in r.iter_content(chunk_size=16384):
                        if not self._running:
                            break
            except Exception as exc:
                log.debug("SI4689 keepalive: %s — relance dans 5s", exc)
            if self._running:
                _time.sleep(5)

        ka_session.close()
        log.info("SI4689 keepalive: arrêté")

    def _boot_fm(self):
        """Charge le firmware FM dans le SI4689 si pas encore booté."""
        try:
            r = self._session.get(f"{self.api_url}/api/status", timeout=3)
            if r.ok:
                body = r.json()
                data = body.get("data", body)
                if data.get("booted"):
                    log.info("SI4689: chip déjà booté")
                    return
        except Exception:
            pass

        # Essayer les endpoints de boot connus
        for endpoint, payload in [
            ("/api/boot",   {"mode": "fm"}),
            ("/api/mode",   {"mode": "fm"}),
            ("/api/source", {"mode": "fm"}),
        ]:
            try:
                r = self._session.post(
                    f"{self.api_url}{endpoint}", json=payload, timeout=10
                )
                if r.ok:
                    log.info("SI4689: boot FM via %s → %s", endpoint, r.text[:80])
                    time.sleep(2)
                    return
            except requests.RequestException:
                continue

        log.warning("SI4689: endpoint boot non trouvé — chip peut-être déjà actif")

    def stop(self):
        self._running = False
        self._stop_radio_py()
        self._session.close()
        log.info("SI4689: arrêté")

    # ------------------------------------------------------------------
    # Gestion du sous-process radio.py
    # ------------------------------------------------------------------

    def _start_radio_py(self) -> bool:
        # Déjà en cours
        if self._radio_proc and self._radio_proc.poll() is None:
            return True

        # Tuer tout process radio.py zombie sur ce port
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
            log.error("→ Vérifiez 'tuner.radio_py_path' dans config.json")
            return False

        try:
            self._radio_proc = subprocess.Popen(
                [sys.executable, self.radio_py, "serve", "--port", str(self.api_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            log.info("SI4689: radio.py démarré (PID %d) sur port %d",
                     self._radio_proc.pid, self.api_port)
            return True
        except Exception as e:
            log.error("SI4689: impossible de démarrer radio.py : %s", e)
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
            log.info("SI4689: radio.py arrêté")
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._radio_proc.pid), signal.SIGKILL)
            except Exception:
                pass
            log.warning("SI4689: radio.py tué (SIGKILL)")
        except Exception as e:
            log.warning("SI4689: erreur arrêt radio.py : %s", e)
        finally:
            self._radio_proc = None

    def _ensure_radio_running(self) -> bool:
        """Relance radio.py s'il a planté."""
        if self._radio_proc and self._radio_proc.poll() is None:
            return True
        log.warning("SI4689: radio.py mort — relance automatique")
        if self._start_radio_py():
            return self._wait_for_api(timeout=10)
        return False

    # ------------------------------------------------------------------
    # Tuning
    # ------------------------------------------------------------------

    def tune(self, frequency_mhz: float) -> bool:
        freq_khz    = int(frequency_mhz * 1000)
        station_id  = f"fm:{freq_khz}"
        freq_hz     = freq_khz * 1000

        # Endpoint prioritaire : play par station_id (fm:88600)
        for endpoint, payload in [
            ("/api/stations/play",  {"station_id": station_id}),
            ("/api/play",           {"station_id": station_id}),
            ("/api/play",           {"frequency": freq_hz, "mode": "fm"}),
            ("/api/tune",           {"station_id": station_id}),
        ]:
            try:
                r = self._session.post(
                    f"{self.api_url}{endpoint}", json=payload, timeout=5
                )
                if r.ok:
                    log.info("SI4689: syntonisé %s via POST %s", station_id, endpoint)
                    return True
                log.debug("SI4689 tune POST %s → %d %s",
                          endpoint, r.status_code, r.text[:60])
            except requests.RequestException as exc:
                log.debug("SI4689 tune POST %s → %s", endpoint, exc)

        # Vérifier si le chip est déjà sur la bonne fréquence
        try:
            r = self._session.get(f"{self.api_url}/api/status", timeout=3)
            if r.ok:
                body = r.json()
                data = body.get("data", body)
                current = data.get("current_station") or {}
                if current.get("freq_khz") == freq_khz:
                    log.info("SI4689: chip déjà syntonisé sur %s", station_id)
                    return True
        except Exception:
            pass

        log.warning("SI4689: syntonisation impossible — chip peut-être déjà sur %.1f MHz",
                    frequency_mhz)
        return True  # booté FM, on continue quand même

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
            # radio.py enveloppe toutes ses réponses dans {"ok": true, "data": {...}}
            data = body.get("data", body)
            status = self._parse_status(data)
            self._last_status = status
            return status
        except requests.RequestException as exc:
            log.debug("SI4689 status error: %s", exc)
            return self._last_status or self._empty_status()

    def _parse_status(self, data: dict) -> dict:
        # Les métriques RF sont dans data["signal"]
        sig = data.get("signal") or {}
        rssi = self._to_float(sig.get("rssi"))
        # valid/acq souvent False en FM analogique même avec bon signal
        # On utilise rssi > 10 comme indicateur de signal présent
        signal_ok = bool(sig.get("acq") or sig.get("valid") or (rssi is not None and rssi >= 10))
        return {
            "signal_dbf":     rssi,
            "snr":            self._to_float(sig.get("snr")),
            "multipath":      self._to_float(sig.get("multipath")),
            "offset_hz":      self._to_int(sig.get("freqoff") or sig.get("freq_offset")),
            "stereo_present": bool(sig.get("stereo", False)),
            "signal_ok":      signal_ok,
            "rds_ta":         bool(data.get("ta", False)),
            "rds_tp":         bool(data.get("tp", False)),
            "tuner_type":     "si4689",
        }

    def _empty_status(self) -> dict:
        return {
            "signal_dbf": None, "snr": None, "multipath": None,
            "offset_hz": None, "stereo_present": None, "signal_ok": False,
            "rds_ta": None, "rds_tp": None, "tuner_type": "si4689",
        }

    # ------------------------------------------------------------------
    # RDS
    # ------------------------------------------------------------------

    def get_rds(self) -> dict:
        if not self._ensure_radio_running():
            return self._last_rds
        try:
            r = self._session.get(f"{self.api_url}/api/status", timeout=3)
            if not r.ok:
                return self._last_rds
            body = r.json()
            data = body.get("data", body)
            rds = self._parse_rds(data)
            if any(v for v in rds.values() if v):
                self._last_rds = rds
            return rds
        except requests.RequestException:
            return self._last_rds

    def _parse_rds(self, data: dict) -> dict:
        # RDS peut être dans data["rds"] ou data["current_station"]
        rds_obj  = data.get("rds") or {}
        station  = data.get("current_station") or {}
        return {
            "ps":  self._clean_str(rds_obj.get("ps")  or station.get("ps")),
            "rt":  self._clean_str(rds_obj.get("rt")  or station.get("rt")),
            "pi":  self._clean_str(rds_obj.get("pi")  or station.get("pi")),
            "pty": self._to_int(rds_obj.get("pty")),
            "ta":  bool(rds_obj.get("ta") or data.get("ta", False)),
            "tp":  bool(rds_obj.get("tp") or data.get("tp", False)),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    @staticmethod
    def _clean_str(v) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None
