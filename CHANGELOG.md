# Changelog — BL-FMO-LITE

All notable changes to this project will be documented in this file.
Format: [Semantic Versioning](https://semver.org)

## [0.1.0] — 2026-05-04

### Added
- Initial release forked from BL-FMO (FM Monitor) v0.6.0
- Tuner abstraction layer (`tuners/base.py`, `tuners/__init__.py`)
- SI4689 backend via Raspiaudio radio.py HTTP API (`tuners/si4689.py`)
- TEF6686 stub (`tuners/tef6686.py`) — hardware incoming
- Dashboard without MPX: Qualité RF card (SNR, Multipath, Offset, Puissance, Stéréo/Mono, TA/TP)
- Signal RF in dBf (0–60), always hardware-tuner scale
- Tuner type badge in sidebar
- BL naming convention and ecosystem documentation
