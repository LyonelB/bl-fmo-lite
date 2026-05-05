"""
BL-FMO-LITE — tuners/__init__.py
Tuner factory: returns the correct backend from config.json.
"""

from .base import TunerBase


def get_tuner(config: dict) -> TunerBase:
    """
    Instantiate and return the tuner backend specified in config["tuner"]["type"].

    Supported values:
        "si4689"   — Raspiaudio Digital Radio HAT (SPI, Raspberry Pi)
        "tef6686"  — TEF Headless Lite Se (I2C/SPI, Raspberry Pi)

    Raises ValueError for unknown tuner types.
    """
    tuner_cfg = config.get("tuner", {})
    tuner_type = tuner_cfg.get("type", "").lower()

    if tuner_type == "si4689":
        from .si4689 import SI4689Tuner
        return SI4689Tuner(tuner_cfg)

    if tuner_type == "tef6686":
        from .tef6686 import TEF6686Tuner
        return TEF6686Tuner(tuner_cfg)

    raise ValueError(
        f"Unknown tuner type: '{tuner_type}'. "
        f"Supported: 'si4689', 'tef6686'. "
        f"Check the 'tuner.type' key in config.json."
    )


__all__ = ["TunerBase", "get_tuner"]
