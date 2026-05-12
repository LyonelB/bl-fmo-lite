"""
BL-FMO-LITE — tuners/__init__.py
Factory : retourne le bon backend depuis config["tuner"]["type"].

Tuners supportés :
    si4689            — Raspiaudio Digital Radio HAT (SPI)
    tef_headless_lite — TEF668X Headless USB Lite (XDR-GTK 115200, audio USB)
    tef_headless      — TEF668X Headless USB (XDR-GTK 115200, sans audio USB)
    tef6686           — TEF6686 ESP32 / poste radio (XDR-GTK 19200, sortie jack)
"""
from .base import TunerBase

def get_tuner(config: dict) -> TunerBase:
    tuner_cfg  = config.get("tuner", {})
    tuner_type = tuner_cfg.get("type", "").lower()
    if tuner_type == "si4689":
        from .si4689 import SI4689Tuner
        return SI4689Tuner(tuner_cfg)
    if tuner_type in ("tef_headless_lite", "tef_headless", "tef6686"):
        from .tef import TEFTuner
        return TEFTuner(tuner_cfg)
    raise ValueError(
        f"Unknown tuner type: '{tuner_type}'. "
        f"Supported: si4689, tef_headless_lite, tef_headless, tef6686."
    )

__all__ = ["TunerBase", "get_tuner"]
