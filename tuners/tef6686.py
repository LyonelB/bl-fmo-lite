"""
BL-FMO-LITE — tuners/tef6686.py
Backend for the TEF6686 Headless Lite Se FM tuner.

NOT YET IMPLEMENTED — stub placeholder.
Will be implemented when the hardware is received.

The TEF6686 communicates over I2C (address 0x60 by default).
Useful references:
  - https://github.com/edy555/tef668x (I2C register map)
  - TEF668x Family Application Note (NXP/SOVDAL)

config.json example:
    "tuner": {
        "type": "tef6686",
        "frequency": "88.6M",
        "i2c_bus": 1,
        "i2c_address": "0x60",
        "alsa_device": "plughw:CARD=DAC,DEV=0"
    }
"""

import logging

from .base import TunerBase

log = logging.getLogger(__name__)


class TEF6686Tuner(TunerBase):
    """TEF6686 Headless Lite Se — stub, not yet implemented."""

    def start(self) -> bool:
        log.error("TEF6686 backend not yet implemented.")
        return False

    def stop(self):
        self._running = False

    def tune(self, frequency_mhz: float) -> bool:
        log.error("TEF6686 backend not yet implemented.")
        return False

    def get_status(self) -> dict:
        return {
            "signal_dbf":     None,
            "snr":            None,
            "multipath":      None,
            "offset_hz":      None,
            "stereo_present": None,
            "signal_ok":      False,
            "rds_ta":         None,
            "rds_tp":         None,
            "tuner_type":     "tef6686",
        }

    def get_rds(self) -> dict:
        return {}
