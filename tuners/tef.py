"""
BL-FMO-LITE — tuners/tef.py
Backends pour toutes les variantes TEF (protocole XDR-GTK commun).

Variantes :
    tef_headless_lite  — TEF668X Headless USB Lite (XDR-GTK 115200, audio USB intégré)
    tef_headless       — TEF668X Headless USB (XDR-GTK 115200, sans audio USB)
    tef6686            — TEF6686 ESP32 / poste radio (XDR-GTK 19200, sortie jack)
"""

import logging
import os
import threading
import time
from typing import Optional

from .base import TunerBase

log = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_VARIANTS = {
    'tef_headless_lite': {'baud_rate': 115200, 'usb_audio': True,  'snr_scale': 6, 'handshake': False},
    'tef_headless':      {'baud_rate': 115200, 'usb_audio': False, 'snr_scale': 6, 'handshake': False},
    'tef6686':           {'baud_rate': 115200, 'usb_audio': False, 'snr_scale': 1, 'handshake': True},
}


def _load_tef_driver():
    import importlib.util
    path = os.path.join(_ROOT, "tef_driver.py")
    if not os.path.exists(path):
        raise ImportError(f"tef_driver.py introuvable : {path}")
    spec   = importlib.util.spec_from_file_location("tef_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TEFDriver


class TEFTuner(TunerBase):

    def __init__(self, config: dict):
        super().__init__(config)
        self.port      = config.get("port", "/dev/ttyACM0")
        variant_key    = config.get("type", "tef_headless_lite").lower()
        variant        = _VARIANTS.get(variant_key, _VARIANTS['tef_headless_lite'])
        self.baud_rate  = int(config.get("baud_rate", variant["baud_rate"]))
        self.usb_audio  = variant['usb_audio']
        self.snr_scale  = int(config.get('snr_scale', variant.get('snr_scale', 1)))
        self.handshake  = variant.get('handshake', False)

        self._driver = None
        self._lock   = threading.Lock()
        self._tuning = False

        self._signal_dbf:     Optional[float] = None
        self._snr:            Optional[int]   = None
        self._multipath:      Optional[int]   = None
        self._offset_hz:      Optional[int]   = None
        self._stereo_present: Optional[bool]  = None

        self._pi: Optional[str] = None
        self._ps: Optional[str] = None
        self._rt: Optional[str] = None

        self._last_status: dict = {}
        self._last_rds:    dict = {}

        log.info("TEF: variante=%s baud=%d audio_usb=%s",
                 variant_key, self.baud_rate, self.usb_audio)

    def start(self) -> bool:
        try:
            TEFDriver = _load_tef_driver()
        except ImportError as e:
            log.error("TEF: %s", e)
            return False

        freq_khz = int(self.frequency_mhz * 1000)
        self._driver = TEFDriver(
            port=self.port,
            baud_rate=self.baud_rate,
            handshake=self.handshake,
            on_signal=self._on_signal,
            on_pi=self._on_pi,
            on_ps=self._on_ps,
            on_rt=self._on_rt,
            on_ms=self._on_ms,
        )
        self._driver.start(freq_khz=freq_khz)

        deadline = time.time() + 10
        while time.time() < deadline:
            if self._signal_dbf is not None:
                break
            time.sleep(0.2)

        if self._signal_dbf is None:
            log.warning("TEF: pas de données signal après 5s")

        self._running = True
        log.info("TEF: démarré — %.1f MHz sur %s @ %d baud",
                 self.frequency_mhz, self.port, self.baud_rate)
        return True

    def stop(self):
        self._running = False
        if self._driver:
            self._driver.stop()
            self._driver = None
        log.info("TEF: arrêté")

    def is_running(self) -> bool:
        return self._running or self._tuning

    def tune(self, frequency_mhz: float) -> bool:
        import time as _t
        freq_khz = int(frequency_mhz * 1000)
        if self._driver:
            self._tuning = True   # neutralise le watchdog
            self._signal_dbf = None  # reset signal
            self._driver.tune(freq_khz)
            self.frequency = f'{frequency_mhz}M'
            log.info("TEF: tune → %.1f MHz", frequency_mhz)
            def _clear_tuning():
                _t.sleep(10)
                self._tuning = False
            import threading as _th
            _th.Thread(target=_clear_tuning, daemon=True).start()
            return True
        return False

    def _on_signal(self, dbf, snr, multipath, offset):
        with self._lock:
            self._signal_dbf = dbf
            self._snr        = snr * self.snr_scale
            self._multipath  = multipath
            self._offset_hz  = offset * 100

    def _on_pi(self, pi):
        with self._lock:
            if pi != self._pi:
                log.info("TEF: PI %s → %s", self._pi, pi)
                self._pi = pi

    def _on_ps(self, ps):
        with self._lock:
            self._ps = ps.strip()

    def _on_rt(self, rt):
        with self._lock:
            self._rt = rt.strip()

    def _on_ms(self, stereo):
        with self._lock:
            self._stereo_present = stereo

    def get_status(self) -> dict:
        with self._lock:
            dbf = self._signal_dbf
            status = {
                "signal_dbf":     dbf,
                "snr":            float(self._snr)       if self._snr       is not None else None,
                "multipath":      float(self._multipath) if self._multipath is not None else None,
                "offset_hz":      self._offset_hz,
                "stereo_present": self._stereo_present,
                "signal_ok":      dbf is not None and dbf >= 10.0,
                "rds_ta":         None,
                "rds_tp":         None,
                "tuner_type":     "tef",
            }
        self._last_status = status
        return status

    def get_rds(self) -> dict:
        with self._lock:
            rds = {
                "ps":  self._ps,
                "pi":  self._pi,
                "rt":  self._rt,
                "pty": None,
                "tp":  None,
                "ta":  None,
            }
        if any(v for v in rds.values() if v):
            self._last_rds = rds
        return rds


TEF6686Tuner         = TEFTuner
TEFHeadlessTuner     = TEFTuner
TEFHeadlessLiteTuner = TEFTuner
