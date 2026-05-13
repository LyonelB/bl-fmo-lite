#!/usr/bin/env python3
"""
BL-FMO-LITE — monitor.py
Moteur de surveillance FM pour tuners hardware (SI4689, TEF6686).

Différences avec BL-FMO (fm-monitor) :
  - Pas de pipeline RTL-SDR / rtl_fm / GNU Radio
  - Pas d'analyse MPX (spectre, déviation, pilote, sous-porteuses)
  - Audio : ALSA (I2S du tuner) → ffmpeg → Icecast2, un seul process
  - Signal/RDS : polling du backend tuner via l'interface TunerBase
  - Métriques qualité : SNR, Multipath, Offset, Stéréo (depuis le tuner)
"""

import subprocess
import threading
import queue
import json
import logging
import time
import os
import collections
from datetime import datetime
from email_alert import EmailAlert
from database import FMDatabase
from tuners import get_tuner

logger = logging.getLogger(__name__)


class FMMonitor:
    """
    Moteur de surveillance FM pour BL-FMO-LITE.
    Utilisé à l'identique par app.py (import FMMonitor from monitor).
    """

    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

        self.audio_config  = self.config['audio']
        self.tuner_config  = self.config.get('tuner', {})

        # Instancier le backend tuner
        self.tuner = get_tuner(self.config)

        # Processus ffmpeg (audio ALSA → Icecast)
        self.audio_process = None

        # État
        self.running       = False
        self.signal_ok     = True
        self.modulation_ok = True
        self.rds_ok        = False
        self.rds_ever_received = False
        self.rds_last_seen = None

        # Alertes
        self.silence_start_time    = None
        self.alert_sent            = False
        self.modulation_alert_sent = False
        self.no_modulation_start   = None
        self.rds_alert_sent        = False

        # Seuils
        self.signal_lost_threshold   = float(self.tuner_config.get('signal_threshold_dbf', 10.0))
        self.modulation_threshold    = float(self.audio_config.get('silence_threshold', -50.0))
        self.silence_duration        = int(self.audio_config.get('silence_duration', 30))
        self.modulation_alert_delay  = int(self.audio_config.get('modulation_alert_delay', 30))
        self.rds_timeout             = int(self.audio_config.get('rds_timeout', 120))

        # Historique signal pour le graphique (120s à 2/sec)
        self.signal_history = collections.deque(maxlen=240)

        # Stats (thread-safe)
        self.stats_lock = threading.Lock()
        self.stats = {
            'start_time':      None,
            'uptime':          0,
            'alerts_sent':     0,
            'last_alert':      None,
            'current_level':   0.0,
            'signal_dbf':      0.0,
            'snr':             None,
            'multipath':       None,
            'offset_hz':       None,
            'stereo_present':  None,
            'modulation_active': True,
            'ps':              '-',
            'rt':              '-',
            'pi':              '-',
            'rds_ta':          None,
            'rds_tp':          None,
            'station_logo':    None,
            'status':          'Arrêté',
            'tuner_type':      self.tuner_config.get('type', '—'),
        }

        # Logo
        self._logo_searched    = False
        self._logo_last_attempt = 0
        self._logo_fail_count  = 0
        self._rds_db_reload    = False
        self._rds_lookup       = None

        # Services
        self.vu_meter_enabled = True
        self.audio_enabled    = True
        self.rds_enabled      = True
        self.watchdog_enabled = False

        # Alertes email
        self.email_alert = EmailAlert(config_path)

        # Base de données
        self.db = FMDatabase()
        self.last_db_save = time.time()
        self.db_queue = queue.Queue(maxsize=100)

    # ──────────────────────────────────────────────────────────────────
    # LIFECYCLE
    # ──────────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            logger.warning("Monitor déjà en cours")
            return

        logger.info(f"Démarrage BL-FMO-LITE — tuner {self.tuner_config.get('type')} "
                    f"@ {self.tuner.frequency}")
        self.running = True
        self.stats['start_time'] = datetime.now()
        self.stats['status'] = 'En cours'

        # Démarrer le tuner hardware
        if not self.tuner.start():
            logger.error("Tuner hardware indisponible — relances automatiques en cours")
            self.stats['status'] = 'Tuner indisponible'
            # Ne pas bloquer : les threads de polling réessaieront

        # Pipeline audio ALSA → Icecast
        self._audio_thread = threading.Thread(
            target=self._audio_pipeline, daemon=True, name='audio'
        )
        self._audio_thread.start()

        # Polling status tuner (SNR, dBf, offset…)
        self._status_thread = threading.Thread(
            target=self._poll_status, daemon=True, name='status-poll'
        )
        self._status_thread.start()

        # Polling RDS
        self._rds_thread = threading.Thread(
            target=self._poll_rds, daemon=True, name='rds-poll'
        )
        self._rds_thread.start()

        # Niveau audio ALSA → dBFS
#        self._audio_level_thread = threading.Thread(
#            target=self._audio_level_monitor, daemon=True, name="audio-level"
#        self._audio_level_thread.start()

        # Surveillance signal + alertes
        self._monitor_thread = threading.Thread(
            target=self._monitor_signal, daemon=True, name='monitor'
        )
        self._monitor_thread.start()

        # Watchdog audio
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, daemon=True, name='watchdog'
        )
        self._watchdog_thread.start()

        # BDD writer
        self._db_thread = threading.Thread(
            target=self._db_writer, daemon=True, name='db-writer'
        )
        self._db_thread.start()

        # Watcher logo / rds-station-db
        self._rds_db_watcher_thread = threading.Thread(
            target=self._rds_db_watcher, daemon=True, name='rds-db-watcher'
        )
        self._rds_db_watcher_thread.start()

        logger.info("BL-FMO-LITE démarré")

    def stop(self):
        logger.info("Arrêt du monitor BL-FMO-LITE")
        self.running = False

        self.tuner.stop()

        if self.audio_process:
            try:
                self.audio_process.kill()
            except Exception:
                pass
            self.audio_process = None

        # Réinitialiser l'état
        self.signal_ok             = True
        self.modulation_ok         = True
        self.modulation_alert_sent = False
        self.no_modulation_start   = None
        self.rds_ok                = False
        self.rds_ever_received     = False
        self.rds_alert_sent        = False
        self._logo_searched        = False
        self._logo_last_attempt    = 0
        self._logo_fail_count      = 0
        self._rds_db_reload        = False
        self._rds_lookup           = None

        with self.stats_lock:
            self.stats['station_logo'] = None
            self.stats['status'] = 'Arrêté'

        # Attendre les threads
        for attr in ['_audio_thread', '_status_thread', '_rds_thread',
                     '_monitor_thread', '_watchdog_thread', '_db_thread']:
            t = getattr(self, attr, None)
            if t and t.is_alive():
                t.join(timeout=3)

    # ──────────────────────────────────────────────────────────────────
    # AUDIO PIPELINE : ALSA → ffmpeg → Icecast2
    # ──────────────────────────────────────────────────────────────────

    def _audio_pipeline(self):
        """
        Pipeline audio.
        SI4689 : radio.py sert /audio/live.mp3 directement.
                 Flask proxifie via /stream.mp3 → pas de relay nécessaire.
        Autre tuner : capture ALSA → ffmpeg → Icecast.
        """
        if hasattr(self.tuner, 'stream_url'):
            logger.info("Audio SI4689 : stream proxifié par Flask via %s",
                        self.tuner.stream_url)
            # Pas de process ffmpeg nécessaire — on attend juste
            while self.running:
                time.sleep(5)
        else:
            icecast_url = self.audio_config.get('icecast_url', '')
            alsa_device = getattr(self.tuner, 'alsa_device', '')
            if icecast_url and alsa_device:
                self._alsa_to_icecast()
            else:
                logger.info('Audio désactivé (pas de device ALSA ou URL Icecast)')
                while self.running:
                    time.sleep(10)

    def _relay_stream(self, source_url: str):
        """Relaie un stream HTTP existant vers Icecast2."""
        icecast_url = self.audio_config.get(
            'icecast_url', 'icecast://source:hackme@localhost:8000/stream'
        )
        cmd = (
            f'ffmpeg -hide_banner -loglevel error '
            f'-i "{source_url}" '
            f'-codec:a copy '
            f'-content_type audio/mpeg -f mp3 '
            f'"{icecast_url}"'
        )
        logger.info("Audio relay: %s → Icecast", source_url)
        while self.running:
            try:
                self.audio_process = subprocess.Popen(
                    cmd, shell=True, executable='/bin/bash',
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                _, stderr = self.audio_process.communicate()
                if self.running and stderr:
                    logger.error("ffmpeg relay: %s", stderr.decode(errors='replace')[:200])
            except Exception as e:
                logger.error("Erreur relay audio: %s", e)
            if self.running:
                time.sleep(30)
                logger.info("Relance relay audio…")

    def _alsa_to_icecast(self):
        """Capture ALSA (I2S) → ffmpeg → Icecast2."""
        alsa_device  = self.tuner.alsa_device
        output_rate  = self.audio_config.get('output_rate', '44100')
        icecast_url  = self.audio_config.get(
            'icecast_url', 'icecast://source:hackme@localhost:8000/stream'
        )
        bitrate = self.audio_config.get('bitrate', '128k')
        cmd = (
            f'ffmpeg -hide_banner -loglevel error '
            f'-fflags nobuffer -flags low_delay '
            f'-f alsa -ar 48000 -ac 2 -i {alsa_device} '
            f'-codec:a libmp3lame -b:a {bitrate} -ar {output_rate} -ac 2 '
            f'-content_type audio/mpeg -f mp3 '
            f'"{icecast_url}"'
        )
        logger.info("Audio ALSA: %s → Icecast", alsa_device)
        while self.running:
            try:
                self.audio_process = subprocess.Popen(
                    cmd, shell=True, executable='/bin/bash',
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
                )
                _, stderr = self.audio_process.communicate()
                if self.running and stderr:
                    logger.error("ffmpeg ALSA: %s", stderr.decode(errors='replace')[:200])
            except Exception as e:
                logger.error("Erreur pipeline ALSA: %s", e)
            if self.running:
                time.sleep(30)
                logger.info("Relance ffmpeg ALSA…")

    # ──────────────────────────────────────────────────────────────────
    # POLLING TUNER
    # ──────────────────────────────────────────────────────────────────

    def _poll_status(self):
        """Polling du statut hardware (SNR, dBf, multipath, offset…) à 1 Hz."""
        while self.running:
            try:
                # Relance si le tuner n'a pas encore démarré
                if not self.tuner.is_running():
                    logger.info("Tentative de démarrage du tuner…")
                    if self.tuner.start():
                        logger.info("Tuner démarré avec succès")
                        self.stats['status'] = 'En cours'
                    else:
                        time.sleep(5)
                        continue

                status = self.tuner.get_status()
                raw_dbf   = status.get('signal_dbf')
                dbf       = raw_dbf if raw_dbf is not None else 0.0
                # Si le tuner ne remonte rien encore, on ne touche pas à signal_ok
                if raw_dbf is not None:
                    signal_ok = status.get('signal_ok', dbf >= self.signal_lost_threshold)
                    self.signal_ok = signal_ok
                else:
                    signal_ok = self.signal_ok  # conserver l'état précédent

                with self.stats_lock:
                    self.stats['signal_dbf']        = dbf
                    self.stats['current_level']     = dbf
                    self.stats['snr']               = status.get('snr')
                    self.stats['multipath']         = status.get('multipath')
                    self.stats['offset_hz']         = status.get('offset_hz')
                    self.stats['stereo_present']    = status.get('stereo_present')
                    self.stats['modulation_active'] = signal_ok
                    self.stats['rds_ta']            = status.get('rds_ta')
                    self.stats['rds_tp']            = status.get('rds_tp')

                # Historique pour le graphique
                self.add_signal_sample(dbf)

                # BDD toutes les 5s
                if self.last_db_save and time.time() - self.last_db_save >= 5:
                    try:
                        self.db_queue.put_nowait({'level': dbf, 'signal_ok': signal_ok})
                        self.last_db_save = time.time()
                    except queue.Full:
                        pass

            except Exception as e:
                logger.error(f"Erreur poll status: {e}")
            time.sleep(0.25)

    def _poll_rds(self):
        """Polling RDS à 1 Hz."""
        while self.running:
            try:
                rds = self.tuner.get_rds()
                if not rds:
                    time.sleep(1)
                    continue

                ps = rds.get('ps')
                rt = rds.get('rt')
                pi = rds.get('pi')

                with self.stats_lock:
                    if ps:
                        self.stats['ps'] = ps.strip()
                    if rt:
                        self.stats['rt'] = rt.strip()
                    if pi:
                        old_pi = self.stats.get('pi', '-')
                        new_pi = pi.strip().upper()
                        if new_pi != old_pi and new_pi not in ('', '-'):
                            logger.info(f"PI changé {old_pi} → {new_pi}")
                            self._logo_searched     = False
                            self._logo_last_attempt = 0
                            self.stats['station_logo'] = None
                            self._rds_db_reload     = True
                        self.stats['pi'] = new_pi

                self.rds_last_seen     = time.time()
                self.rds_ever_received = True
                self.rds_ok            = True

                if not self._logo_searched:
                    threading.Thread(
                        target=self._fetch_station_logo, daemon=True
                    ).start()

            except Exception as e:
                logger.error(f"Erreur poll RDS: {e}")
            time.sleep(1)

    # ──────────────────────────────────────────────────────────────────
    # SURVEILLANCE SIGNAL & ALERTES
    # ──────────────────────────────────────────────────────────────────

    def _monitor_signal(self):
        """Surveille le signal et envoie les alertes email."""
        logger.info("Thread surveillance démarré")
        while self.running:
            try:
                with self.stats_lock:
                    dbf       = self.stats['signal_dbf']
                    signal_ok = self.signal_ok

                # ── 1. Perte de signal (porteuse absente) ──────────────────
                if not signal_ok:
                    if self.silence_start_time is None:
                        self.silence_start_time = time.time()
                        logger.warning(f"Perte signal détectée: {dbf:.1f} dBf")
                    else:
                        silence = time.time() - self.silence_start_time
                        if silence >= self.silence_duration and not self.alert_sent:
                            logger.error(f"Signal perdu depuis {silence:.0f}s — ALERTE")
                            self.alert_sent = True  # bloquer AVANT l'envoi
                            ok = self.email_alert.send_alert(
                                alert_type="Émetteur FM hors ligne",
                                details=(
                                    f"Aucune porteuse FM détectée.\n"
                                    f"Niveau : {dbf:.1f} dBf\n"
                                    f"Durée : {int(silence)}s"
                                ),
                                skip_cooldown=True
                            )
                            with self.stats_lock:
                                self.stats['alerts_sent'] += 1
                                self.stats['last_alert'] = datetime.now().isoformat()
                            self.db.save_alert(
                                alert_type='signal_lost',
                                level_db=dbf,
                                duration_seconds=int(silence),
                                message=f"Émetteur hors ligne — {dbf:.1f} dBf",
                                email_sent=ok
                            )
                else:
                    if self.silence_start_time is not None:
                        logger.info(f"Signal rétabli: {dbf:.1f} dBf")
                        if self.alert_sent:
                            self.email_alert.send_recovery_alert()
                            self.db.save_alert(
                                alert_type='signal_restored',
                                level_db=dbf,
                                duration_seconds=int(time.time() - self.silence_start_time),
                                message=f"Signal rétabli — {dbf:.1f} dBf",
                                email_sent=True
                            )
                    self.silence_start_time = None
                    self.alert_sent = False

                # ── 2. RDS timeout ─────────────────────────────────────────
                if self.rds_enabled and self.rds_ever_received and self.rds_last_seen:
                    absence = time.time() - self.rds_last_seen
                    if absence >= self.rds_timeout:
                        if not self.rds_alert_sent:
                            self.rds_ok = False
                            logger.warning(f"RDS absent depuis {absence:.0f}s — ALERTE")
                            ok = self.email_alert.send_alert(
                                alert_type="Signal RDS absent",
                                details=f"Aucune donnée RDS reçue depuis {int(absence)}s.",
                                skip_cooldown=True
                            )
                            if ok:
                                self.rds_alert_sent = True
                                self.db.save_alert(
                                    alert_type='rds_lost',
                                    level_db=dbf,
                                    duration_seconds=int(absence),
                                    message=f"RDS absent {int(absence)}s",
                                    email_sent=True
                                )
                    else:
                        if self.rds_alert_sent:
                            logger.info("RDS rétabli")
                            self.email_alert.send_alert(
                                alert_type="Signal RDS rétabli",
                                details="Les données RDS sont à nouveau reçues.",
                                skip_cooldown=True
                            )
                            self.db.save_alert(
                                alert_type='rds_restored',
                                level_db=dbf,
                                duration_seconds=0,
                                message="RDS rétabli",
                                email_sent=True
                            )
                        self.rds_ok = True
                        self.rds_alert_sent = False

                # ── Uptime ─────────────────────────────────────────────────
                if self.stats['start_time']:
                    uptime = (datetime.now() - self.stats['start_time']).total_seconds()
                    with self.stats_lock:
                        self.stats['uptime'] = int(uptime)

            except Exception as e:
                logger.error(f"Erreur surveillance: {e}")

            time.sleep(1)

    # ──────────────────────────────────────────────────────────────────
    # WATCHDOG
    # ──────────────────────────────────────────────────────────────────

    def _watchdog(self):
        """Relance le pipeline audio si ffmpeg plante."""
        logger.info("Watchdog démarré")
        while self.running:
            time.sleep(15)
            if not self.running:
                break
            if not self.watchdog_enabled:
                continue
            if self.audio_process and self.audio_process.poll() is not None:
                logger.error("ffmpeg audio planté — relance via _audio_pipeline")
                # Le thread _audio_pipeline gère la relance lui-même

    # ──────────────────────────────────────────────────────────────────
    # BDD WRITER
    # ──────────────────────────────────────────────────────────────────

    def _db_writer(self):
        while self.running:
            try:
                item = self.db_queue.get(timeout=1)
                self.db.save_audio_level(item['level'], item['signal_ok'])
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Erreur BDD: {e}")

    # ──────────────────────────────────────────────────────────────────
    # LOGO / RDS-STATION-DB
    # ──────────────────────────────────────────────────────────────────

    def _get_rds_lookup(self, force_refresh=False):
        try:
            from rds_lookup import RDSLookup
            if self._rds_lookup is None:
                self._rds_lookup = RDSLookup(country='FR', auto_refresh=False)
            if force_refresh:
                self._rds_lookup.force_refresh()
            return self._rds_lookup
        except Exception as e:
            logger.debug(f"rds_lookup indisponible: {e}")
            return None

    def _rds_db_watcher(self):
        """Recharge rds-station-db toutes les 24h ou sur changement de PI."""
        INTERVAL = 24 * 3600
        while self.running:
            elapsed = 0
            while self.running and elapsed < INTERVAL:
                time.sleep(60)
                elapsed += 60
                if self._rds_db_reload:
                    self._rds_db_reload = False
                    break
            if not self.running:
                break
            try:
                pi = self.stats.get('pi', '').strip().upper()
                ps = self.stats.get('ps', '').strip()
                if not pi or pi == '-':
                    continue
                lookup = self._get_rds_lookup(force_refresh=True)
                if not lookup:
                    continue
                station = lookup.get(pi=pi, ps=ps) if ps else lookup.get_by_pi(pi)
                if not station:
                    continue
                new_logo = station.get('logo_url')
                if new_logo and new_logo != self.stats.get('station_logo'):
                    logger.info(f"Watcher: logo mis à jour [{pi}] → {new_logo}")
                    with self.stats_lock:
                        self.stats['station_logo'] = new_logo
            except Exception as e:
                logger.warning(f"Watcher rds-station-db: {e}")

    def _fetch_station_logo(self):
        if time.time() - self._logo_last_attempt < 60:
            return
        self._logo_last_attempt = time.time()
        try:
            # Attendre que PI et PS soient disponibles
            for _ in range(10):
                pi = self.stats.get('pi', '').strip().upper()
                ps = self.stats.get('ps', '').strip()
                if pi and pi != '-' and ps and ps != '-':
                    break
                time.sleep(0.5)

            pi = self.stats.get('pi', '').strip().upper()
            ps = self.stats.get('ps', '').strip()
            if not pi or pi == '-' or not ps or ps == '-':
                return

            lookup = self._get_rds_lookup(force_refresh=True)
            if not lookup:
                return

            station = lookup.get(pi=pi, ps=ps)
            if station and station.get('logo_url'):
                logo_url = station['logo_url']
                logger.info(f"Logo trouvé [{pi}/{ps}]: {logo_url}")
                self._logo_searched = True
                with self.stats_lock:
                    self.stats['station_logo'] = logo_url
            else:
                self._logo_fail_count += 1
                logger.info(f"Aucun logo [{pi}/{ps}] (#{self._logo_fail_count})")
                self._logo_searched = True

        except Exception as e:
            logger.warning(f"Erreur logo: {e}")

    # ──────────────────────────────────────────────────────────────────
    # API PUBLIQUE (compatibilité app.py)
    # ──────────────────────────────────────────────────────────────────

    def _audio_level_monitor(self):
        """
        Lit le stream HTTP de radio.py directement via ffmpeg → PCM → RMS → dBFS.
        Identique à FM Monitor. Connexion independante du relay browser.
        """
        import numpy as np, subprocess as _sp
        stream_url = getattr(self.tuner, 'stream_url', None)
        if not stream_url:
            logger.info('Audio level monitor: desactive (pas de stream_url)')
            return
        logger.info(f'Audio level monitor: {stream_url}')
        while self.running:
            try:
                proc = _sp.Popen(
                    ['ffmpeg', '-hide_banner', '-loglevel', 'error',
                     '-i', stream_url,
                     '-f', 's16le', '-ar', '44100', '-ac', '1', 'pipe:1'],
                    stdout=_sp.PIPE, stderr=_sp.DEVNULL
                )
                logger.info('Audio level monitor: demarre')
                chunk_size = 2048  # ~23ms a 44100Hz
                while self.running:
                    raw = proc.stdout.read(chunk_size)
                    if not raw or len(raw) < chunk_size:
                        break
                    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
                    rms = np.sqrt(np.mean(samples ** 2))
                    dbfs = 20 * np.log10(rms / 32768.0) if rms > 0 else -100.0
                    self.add_signal_sample(round(dbfs, 1))
                proc.kill()
            except Exception as e:
                logger.error(f'Audio level monitor error: {e}')
            if self.running:
                time.sleep(5)
    def get_stats(self) -> dict:
        with self.stats_lock:
            stats = self.stats.copy()

        stats['signal_ok']         = self.signal_ok
        stats['modulation_ok']     = self.modulation_ok
        stats['rds_ok']            = self.rds_ok
        stats['rds_ever_received'] = self.rds_ever_received
        stats['frequency']         = self.tuner.frequency
        stats['mpx_enabled']       = False   # Pas de MPX en mode LITE

        if stats['start_time']:
            stats['start_time'] = stats['start_time'].strftime('%d/%m/%Y %H:%M:%S')

        return stats

    def get_services_status(self) -> dict:
        return {
            'vu_meter':  self.vu_meter_enabled,
            'audio':     self.audio_enabled,
            'watchdog':  self.watchdog_enabled,
            'rds':       self.rds_enabled,
            'history':   False,
            'mpx':       False,
            'monitoring': self.running,
        }

    def toggle_service(self, service: str, enabled: bool) -> bool:
        if service == 'watchdog':
            self.watchdog_enabled = enabled
        elif service == 'rds':
            self.rds_enabled = enabled
        elif service == 'audio':
            self.audio_enabled = enabled
        else:
            logger.warning(f"Service inconnu ou non applicable en mode LITE: {service}")
            return False
        logger.info(f"Service {service} → {'activé' if enabled else 'désactivé'}")
        return True

    def add_signal_sample(self, level: float):
        self.signal_history.append({'t': int(time.time() * 1000), 'l': round(level, 1)})

    def get_signal_history(self) -> list:
        return list(self.signal_history)

    # Stub pour compatibilité app.py
    def read_rds_once(self, duration=10):
        """Sans objet en mode LITE (polling continu)."""
        logger.info("read_rds_once: no-op en mode LITE (RDS en polling continu)")
        return True
