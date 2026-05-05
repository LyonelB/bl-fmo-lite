"""
BL-FMO-LITE — tuners/base.py
Abstract base class for all tuner backends.

Every tuner backend must implement this interface.
Fields returned by get_status() are consumed directly by the Flask API (/api/stats)
and by the SSE stream — do not rename keys without updating the frontend.
"""

from abc import ABC, abstractmethod


class TunerBase(ABC):
    """
    Common interface for all FM tuner backends.

    Concrete implementations: SI4689 (Raspiaudio HAT), TEF6686 (TEF Headless Lite Se).
    """

    def __init__(self, config: dict):
        self.config = config
        self.frequency = config.get("frequency", "88.6M")
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    def start(self) -> bool:
        """
        Initialise the tuner hardware and tune to the configured frequency.
        Returns True on success, False on failure.
        """

    @abstractmethod
    def stop(self):
        """Release hardware resources cleanly."""

    # ------------------------------------------------------------------
    # Tuning
    # ------------------------------------------------------------------

    @abstractmethod
    def tune(self, frequency_mhz: float) -> bool:
        """
        Tune to frequency_mhz (e.g. 88.6).
        Returns True on success.
        """

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @abstractmethod
    def get_status(self) -> dict:
        """
        Return a dict with at least these keys (None if unavailable):

            signal_dbf      float | None   — field strength in dBµV/m (0-60)
            snr             float | None   — SNR in dB (0-60)
            multipath       float | None   — multipath index 0-100 %
            offset_hz       int   | None   — frequency offset in Hz
            stereo_present  bool  | None   — True = stereo
            rds_ta          bool  | None   — Traffic Announcement flag
            rds_tp          bool  | None   — Traffic Programme flag

        Additional keys are allowed and will be forwarded to the frontend.
        """

    # ------------------------------------------------------------------
    # RDS
    # ------------------------------------------------------------------

    @abstractmethod
    def get_rds(self) -> dict:
        """
        Return a dict with at least:

            ps      str | None   — Programme Service name (8 chars)
            rt      str | None   — RadioText (64 chars)
            pi      str | None   — Programme Identification (hex, e.g. "FA41")
            pty     int | None   — Programme Type (0-31)
            ta      bool | None  — Traffic Announcement
            tp      bool | None  — Traffic Programme

        Returns empty dict {} if RDS is not available.
        """

    # ------------------------------------------------------------------
    # Audio source
    # ------------------------------------------------------------------

    @property
    def alsa_device(self) -> str:
        """
        ALSA capture device for this tuner's I2S output.
        Default: 'default'. Override in subclass as needed.
        Example: 'plughw:CARD=si4689_i2s,DEV=0'
        """
        return self.config.get("alsa_device", "default")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def frequency_mhz(self) -> float:
        """Parse frequency from config string, e.g. '88.6M' -> 88.6"""
        freq = str(self.frequency).upper().replace("M", "").strip()
        try:
            return float(freq)
        except ValueError:
            return 88.6

    def is_running(self) -> bool:
        return self._running
