#!/usr/bin/env python3
"""
BL-FMO-LITE — app.py
Application Flask pour le monitoring FM sur tuners hardware.
"""

from flask import (Flask, render_template, Response, jsonify,
                   request, session, redirect, url_for)
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, generate_csrf
import logging
import time
import json
import subprocess
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from monitor import FMMonitor
from auth import Auth

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32).hex()

bcrypt    = Bcrypt(app)
csrf      = CSRFProtect(app)
limiter   = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)
auth = Auth()

SESSION_TIMEOUT = timedelta(minutes=60)

@app.before_request
def check_session_timeout():
    if request.endpoint in ('stream_stats', 'proxy_stream', 'static',
                            'setup_page', 'scan_tuners', 'setup_apply'):
        return
    # Redirection setup si pas de config
    if not _has_valid_config() and request.endpoint not in ('login', 'logout', None):
        if request.is_json or (request.path.startswith('/api/') and request.endpoint):
            return jsonify({'status': 'error', 'message': 'Setup required'}), 503
        return redirect(url_for('setup_page'))
    if session.get('logged_in'):
        last_active = session.get('last_active')
        if last_active:
            elapsed = datetime.utcnow() - datetime.fromisoformat(last_active)
            if elapsed > SESSION_TIMEOUT:
                session.clear()
                if request.is_json:
                    from flask import abort
                    abort(401)
                return redirect(url_for('login', timeout=1))
        session['last_active'] = datetime.utcnow().isoformat()

monitor = None
stats_cache = {'data': None, 'timestamp': 0}

def generate_stats_sse():
    while True:
        try:
            if monitor:
                data = monitor.get_stats()
                yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.05)
        except GeneratorExit:
            break
        except Exception as e:
            logger.error(f"Erreur SSE: {e}")
            time.sleep(0.1)

# ──────────────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")
def login():
    if request.method == 'POST':
        if request.is_json:
            data = request.get_json()
            username = data.get('username')
            password = data.get('password')
        else:
            username = request.form.get('username')
            password = request.form.get('password')

        if auth.verify_credentials(username, password):
            session['logged_in'] = True
            session['username']  = username
            session['last_active'] = datetime.utcnow().isoformat()
            session.permanent = True
            logger.info(f"Connexion réussie: {username}")
            next_page = request.args.get('next', '/')
            if request.is_json:
                return jsonify({'status': 'success', 'redirect': next_page})
            return redirect(next_page)
        else:
            logger.warning(f"Échec connexion: {username}")
            if request.is_json:
                return jsonify({'status': 'error',
                                'message': "Identifiants incorrects"}), 401
            return render_template('login.html', error='Identifiants incorrects')

    return render_template('login.html')

@app.route('/logout')
def logout():
    username = session.get('username', 'unknown')
    session.clear()
    logger.info(f"Déconnexion: {username}")
    return redirect(url_for('login'))

@app.route('/api/csrf-token')
def get_csrf_token():
    return jsonify({'csrf_token': generate_csrf()})

# ──────────────────────────────────────────────────────────────────────
# PAGES
# ──────────────────────────────────────────────────────────────────────

@app.route('/')
@auth.login_required
def index():
    return render_template('index.html')

@app.route('/config')
@auth.login_required
def config():
    return render_template('config.html')

@app.route('/stats')
@auth.login_required
def stats():
    return render_template('stats.html')

@app.route('/about')
@auth.login_required
def about_page():
    return render_template('about.html')

# ──────────────────────────────────────────────────────────────────────
# API STATS
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/stats')
@limiter.exempt
def get_stats():
    now = time.time()
    if stats_cache['data'] and (now - stats_cache['timestamp']) < 0.1:
        return jsonify(stats_cache['data'])
    if monitor:
        data = monitor.get_stats()
        stats_cache['data']      = data
        stats_cache['timestamp'] = now
        return jsonify(data)
    return jsonify({'error': 'Monitor not initialized'}), 503

@app.route('/api/stream/stats')
@limiter.exempt
@csrf.exempt
def stream_stats():
    return Response(
        generate_stats_sse(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

@app.route('/api/signal/history')
@auth.login_required
@limiter.exempt
def get_signal_history():
    if monitor:
        return jsonify(monitor.get_signal_history())
    return jsonify([])

# ──────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/config/full')
def get_config_full():
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        if 'email' in cfg and 'sender_password' in cfg['email']:
            cfg['email']['sender_password'] = '********'
        return jsonify(cfg)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/save', methods=['POST'])
@auth.login_required
def save_config():
    try:
        data = request.get_json()
        with open('config.json', 'r') as f:
            cfg = json.load(f)

        # Station
        if 'station' in data:
            cfg.setdefault('station', {})
            if 'name' in data['station']:
                cfg['station']['name'] = data['station']['name']
            if 'frequency' in data['station']:
                cfg['station']['frequency_display'] = data['station']['frequency']

        # Tuner (remplace rtl_sdr dans BL-FMO)
        if 'tuner' in data:
            cfg.setdefault('tuner', {})
            for key in ('frequency', 'type', 'api_url', 'alsa_device',
                        'signal_threshold_dbf'):
                if key in data['tuner']:
                    cfg['tuner'][key] = data['tuner'][key]

        # Audio
        if 'audio' in data:
            cfg.setdefault('audio', {})
            for key in ('silence_threshold', 'silence_duration',
                        'modulation_alert_delay', 'rds_timeout',
                        'output_rate', 'bitrate', 'icecast_url'):
                if key in data['audio']:
                    cfg['audio'][key] = data['audio'][key]
            if monitor:
                monitor.silence_duration       = int(cfg['audio'].get('silence_duration', 30))
                monitor.modulation_alert_delay = int(cfg['audio'].get('modulation_alert_delay', 30))
                monitor.rds_timeout            = int(cfg['audio'].get('rds_timeout', 120))

        # Email
        if 'email' in data:
            cfg.setdefault('email', {})
            for key in ('sender_email', 'enabled', 'smtp_server',
                        'smtp_port', 'use_tls', 'cooldown_minutes'):
                if key in data['email']:
                    cfg['email'][key] = data['email'][key]
            if 'sender_password' in data['email']:
                pwd = data['email']['sender_password']
                if pwd and pwd.strip() and pwd.strip() != '********':
                    cfg['email']['sender_password'] = pwd.replace(' ', '')
            if 'recipient_emails' in data['email']:
                emails = data['email']['recipient_emails']
                if isinstance(emails, str):
                    emails = [e.strip() for e in emails.split(',') if e.strip()]
                cfg['email']['recipient_emails'] = emails

        # Auth
        if 'auth' in data:
            cfg.setdefault('auth', {})
            if data['auth'].get('username'):
                cfg['auth']['username'] = data['auth']['username']
            if data['auth'].get('password'):
                cfg['auth']['password_hash'] = bcrypt.generate_password_hash(
                    data['auth']['password']
                ).decode('utf-8')
                global auth
                auth = Auth()

        with open('config.json', 'w') as f:
            json.dump(cfg, f, indent=2)

        logger.info("Configuration sauvegardée")

        # Redémarrer le monitor si la fréquence a changé
        if 'tuner' in data and 'frequency' in data['tuner'] and monitor:
            logger.info("Fréquence modifiée — redémarrage monitor")
            monitor.stop()
            time.sleep(2)
            monitor.config = cfg
            monitor.tuner_config = cfg.get('tuner', {})
            monitor.start()

        return jsonify({'status': 'success', 'message': 'Configuration enregistrée'})

    except Exception as e:
        logger.error(f"Erreur save_config: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# LOGS & SERVICES
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/logs')
@auth.login_required
def get_logs():
    try:
        result = subprocess.run(
            ['journalctl', '-u', 'bl-fmo-lite', '-n', '100', '--no-pager'],
            capture_output=True, text=True
        )
        return jsonify({'logs': result.stdout})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/services/status')
def get_services_status():
    if monitor:
        return jsonify(monitor.get_services_status())
    return jsonify({'monitoring': False}), 503

@app.route('/api/services/toggle', methods=['POST'])
@auth.login_required
def toggle_service():
    try:
        data    = request.get_json()
        service = data.get('service')
        enabled = data.get('enabled', True)
        if monitor:
            ok = monitor.toggle_service(service, enabled)
            if ok:
                return jsonify({'status': 'success'})
        return jsonify({'status': 'error', 'message': 'Service inconnu'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/restart', methods=['POST'])
@auth.login_required
def restart_monitoring():
    global monitor
    try:
        if monitor:
            monitor.stop()
            time.sleep(2)
            monitor.start()
            return jsonify({'status': 'success', 'message': 'Monitor redémarré'})
        return jsonify({'status': 'error', 'message': 'Monitor not initialized'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# EMAIL
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/test-email', methods=['POST'])
@auth.login_required
def test_email():
    try:
        if monitor and monitor.email_alert:
            ok = monitor.email_alert.send_alert(
                alert_type="Test",
                details="Email de test depuis BL-FMO-LITE.",
                skip_cooldown=True
            )
            if ok:
                return jsonify({'status': 'success', 'message': 'Email envoyé'})
            return jsonify({'status': 'error', 'message': "Erreur d'envoi"}), 500
        return jsonify({'status': 'error', 'message': 'Monitor not initialized'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# HISTORIQUE BDD
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/audio/history')
@auth.login_required
@limiter.exempt
def get_audio_history():
    try:
        if monitor and hasattr(monitor, 'db'):
            history = monitor.db.get_audio_history(hours=24)
            return jsonify({'status': 'success', 'data': history})
        return jsonify({'status': 'error', 'message': 'Database not available'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/alerts/history')
@auth.login_required
def get_alerts_history():
    try:
        if monitor and hasattr(monitor, 'db'):
            alerts = monitor.db.get_alerts_history(limit=50)
            return jsonify({'status': 'success', 'data': alerts})
        return jsonify({'status': 'error', 'message': 'Database not available'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/alerts/history/grouped')
@auth.login_required
@limiter.exempt
def get_alerts_history_grouped():
    try:
        if monitor and hasattr(monitor, 'db'):
            alerts = monitor.db.get_alerts_history_grouped(limit=50)
            return jsonify({'status': 'success', 'data': alerts})
        return jsonify({'status': 'error', 'message': 'Database not available'}), 503
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# STREAM AUDIO & ENREGISTREMENT
# ──────────────────────────────────────────────────────────────────────

@app.route('/stream.mp3')
@limiter.exempt
def proxy_stream():
    """Proxifie le stream audio vers le navigateur."""
    import requests as req

    # SI4689 : radio.py fournit son propre stream
    if monitor and hasattr(monitor.tuner, 'stream_url'):
        source = monitor.tuner.stream_url
    else:
        # Autre tuner : stream Icecast local
        try:
            with open('config.json', 'r') as f:
                cfg = json.load(f)
            raw = cfg.get('audio', {}).get(
                'icecast_url', 'http://localhost:8000/stream'
            )
            if raw.startswith('icecast://'):
                parts = raw.replace('icecast://', '').split('@', 1)
                source = 'http://' + (parts[1] if len(parts) > 1 else parts[0])
            else:
                source = raw
        except Exception:
            source = 'http://localhost:8000/stream'

    def generate():
        try:
            with req.get(source, stream=True, timeout=5) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
        except Exception as e:
            logger.error(f"Erreur proxy stream ({source}): {e}")

    return app.response_class(
        generate(),
        mimetype='audio/mpeg',
        headers={
            'Cache-Control': 'no-cache, no-store',
            'Access-Control-Allow-Origin': '*'
        }
    )

record_process  = None
record_filepath = None
RECORD_MAX_BYTES = 50 * 1024 * 1024
RECORD_DIR = '/tmp'

@app.route('/api/record/start', methods=['POST'])
@auth.login_required
def record_start():
    global record_process, record_filepath
    if record_process and record_process.poll() is None:
        return jsonify({'status': 'error', 'message': 'Enregistrement déjà en cours'}), 400

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    record_filepath = os.path.join(RECORD_DIR, f'bl-fmo-lite_{timestamp}.mp3')

    try:
        # Déterminer l'URL Icecast pour l'enregistrement
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        icecast_src = cfg.get('audio', {}).get(
            'icecast_url', 'icecast://source:hackme@localhost:8000/stream'
        )
        icecast_http = icecast_src.replace('icecast://', 'http://')
        if '@' in icecast_http:
            icecast_http = 'http://' + icecast_http.split('@', 1)[1]

        record_process = subprocess.Popen([
            'ffmpeg', '-y', '-i', icecast_http,
            '-c', 'copy', '-fs', str(RECORD_MAX_BYTES),
            record_filepath
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({'status': 'ok', 'message': 'Enregistrement démarré'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/record/stop', methods=['POST'])
@auth.login_required
def record_stop():
    global record_process, record_filepath
    if not record_process or record_process.poll() is not None:
        return jsonify({'status': 'error', 'message': 'Aucun enregistrement'}), 400

    record_process.terminate()
    try:
        record_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        record_process.kill()
    record_process = None

    if record_filepath and os.path.exists(record_filepath):
        size = os.path.getsize(record_filepath)
        return jsonify({'status': 'ok', 'filepath': record_filepath, 'size': size})
    return jsonify({'status': 'error', 'message': 'Fichier introuvable'}), 500

@app.route('/api/record/download')
@auth.login_required
def record_download():
    global record_filepath
    if not record_filepath or not os.path.exists(record_filepath):
        return jsonify({'status': 'error', 'message': 'Aucun fichier'}), 404

    filepath = record_filepath
    filename = os.path.basename(filepath)
    record_filepath = None

    from flask import send_file
    import threading

    def _delete(path):
        time.sleep(5)
        try:
            os.remove(path)
        except Exception:
            pass

    threading.Thread(target=_delete, args=(filepath,), daemon=True).start()
    return send_file(filepath, mimetype='audio/mpeg',
                     as_attachment=True, download_name=filename)

@app.route('/api/record/status')
@auth.login_required
def record_status():
    global record_process, record_filepath
    recording = record_process is not None and record_process.poll() is None
    size = 0
    if record_filepath and os.path.exists(record_filepath):
        size = os.path.getsize(record_filepath)
    if recording and size >= RECORD_MAX_BYTES:
        record_process.terminate()
        try:
            record_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record_process.kill()
        record_process = None
        recording = False
    return jsonify({'recording': recording, 'size': size, 'max': RECORD_MAX_BYTES})

# ──────────────────────────────────────────────────────────────────────
# VOLUME ALSA (sortie I2S ou DAC)
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/volume')
@auth.login_required
def get_volume():
    """Lit le volume ALSA de la sortie configurée."""
    try:
        import re
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        alsa_card    = cfg.get('audio', {}).get('alsa_mixer_card', 'DAC')
        alsa_control = cfg.get('audio', {}).get('alsa_mixer_control', 'Digital')
        result = subprocess.run(
            ['amixer', '-c', alsa_card, 'sget', alsa_control],
            capture_output=True, text=True, timeout=5
        )
        match = re.search(r'\[(\d+)%\]', result.stdout)
        if match:
            return jsonify({'status': 'success', 'volume': int(match.group(1))})
        return jsonify({'status': 'error', 'message': 'Impossible de lire le volume'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/volume/set', methods=['POST'])
@auth.login_required
def set_volume():
    try:
        data    = request.get_json()
        volume  = max(0, min(100, int(data.get('volume', 80))))
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        alsa_card    = cfg.get('audio', {}).get('alsa_mixer_card', 'DAC')
        alsa_control = cfg.get('audio', {}).get('alsa_mixer_control', 'Digital')
        result = subprocess.run(
            ['amixer', '-c', alsa_card, 'sset', alsa_control, f'{volume}%'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return jsonify({'status': 'success', 'volume': volume})
        return jsonify({'status': 'error', 'message': result.stderr}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# PRESETS
# ──────────────────────────────────────────────────────────────────────

@app.route('/api/presets')
@auth.login_required
def get_presets():
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
        return jsonify(cfg.get('presets', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ──────────────────────────────────────────────────────────────────────
# DÉMARRAGE
# ──────────────────────────────────────────────────────────────────────

def _has_valid_config():
    """Retourne True si config.json existe et contient un tuner configuré."""
    if not os.path.exists('config.json'):
        return False
    try:
        with open('config.json') as f:
            cfg = json.load(f)
        return bool(cfg.get('tuner', {}).get('type'))
    except Exception:
        return False


def start_monitor():
    global monitor
    try:
        monitor = FMMonitor('config.json')
        monitor.start()
        logger.info("Monitor démarré")
    except Exception as e:
        logger.error(f"Erreur démarrage monitor: {e}")


if _has_valid_config():
    start_monitor()
else:
    logger.info("Pas de configuration — en attente de setup (/setup)")


# ── Routes Setup ────────────────────────────────────────────────────────

@app.route('/api/select-source', methods=['POST'])
@csrf.exempt
@auth.login_required
def select_source():
    """Change le tuner actif et redémarre le monitor."""
    global monitor
    try:
        data    = request.get_json()
        source  = data.get('source')   # 'tef', 'si4689', 'rtlsdr', 'gnuradio'
        port    = data.get('port', '')

        # Charger la config template correspondante
        if source == 'tef':
            tpl = 'config_tef6686.json'
            tuner_type = 'tef6686'
        else:
            tpl = 'config_si4689.json'
            tuner_type = 'si4689'

        if os.path.exists(tpl):
            with open(tpl) as f:
                cfg = json.load(f)
        else:
            with open('config.json') as f:
                cfg = json.load(f)

        cfg['tuner']['type'] = tuner_type
        if source == 'tef' and port:
            cfg['tuner']['port'] = port

        with open('config.json', 'w') as f:
            json.dump(cfg, f, indent=2)

        # Redémarrer le monitor
        if monitor:
            monitor.stop()
            time.sleep(2)
        start_monitor()

        return jsonify({'status': 'success'})
    except Exception as e:
        logger.error(f'select_source error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/setup')
def setup_page():
    return render_template('setup.html')


@app.route('/api/scan-dongle', methods=['GET','POST'])
@app.route('/api/scan-tuners')
def scan_tuners():
    """Détecte les tuners disponibles — format compatible config.html."""
    import glob, subprocess as _sp

    # TEF6686
    tef_ports = glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*')

    # SI4689
    radio_py = os.path.join(os.path.dirname(__file__),
                            'vendor', 'raspiaudio', 'radio.py')
    alsa_out = _sp.run(['arecord','-l'], capture_output=True, text=True).stdout
    si4689_detected = os.path.exists(radio_py) and 'si4689' in alsa_out.lower()

    return jsonify({
        'tef':    tef_ports,
        'si4689': si4689_detected,
        'rtlsdr': False,
        'tuners': [
            {'type':'tef6686','port':p,'alsa_device':'hw:Tuner','detected':True}
            for p in tef_ports
        ] + ([{
            'type':'si4689','radio_py_path':radio_py,
            'api_port':8686,'alsa_device':'plughw:CARD=si4689i2s,DEV=0',
            'detected':si4689_detected
        }] if os.path.exists(radio_py) else []),
    })


@app.route('/api/setup/apply', methods=['POST'])
@csrf.exempt
def setup_apply():
    """Sauvegarde la config choisie et démarre le monitor."""
    global monitor
    try:
        data = request.get_json()
        tuner_type = data.get('type')
        frequency  = data.get('frequency', '88.6M')
        station    = data.get('station_name', 'Ma Radio')

        # Charger le template de config correspondant
        tpl_path = f'config_{tuner_type}.json'
        if os.path.exists(tpl_path):
            with open(tpl_path) as f:
                cfg = json.load(f)
        else:
            cfg = {}

        # Appliquer les paramètres
        cfg.setdefault('tuner', {})['type']      = tuner_type
        cfg['tuner']['frequency']                = frequency
        cfg.setdefault('station', {})['name']    = station
        cfg['station']['frequency_display']      = frequency.replace('M', ' MHz')

        # Paramètres spécifiques TEF6686
        if tuner_type == 'tef6686':
            cfg['tuner']['port']        = data.get('port', '/dev/ttyACM0')
            cfg['tuner']['alsa_device'] = data.get('alsa_device', 'hw:Tuner')

        # Paramètres spécifiques SI4689
        if tuner_type == 'si4689':
            cfg['tuner']['radio_py_path'] = data.get('radio_py_path',
                'vendor/raspiaudio/radio.py')
            cfg['tuner']['api_port']      = int(data.get('api_port', 8686))
            cfg['tuner']['alsa_device']   = data.get('alsa_device',
                'plughw:CARD=si4689i2s,DEV=0')

        # Sauvegarder
        with open('config.json', 'w') as f:
            json.dump(cfg, f, indent=2)

        # Arrêter l'ancien monitor si actif
        if monitor:
            monitor.stop()
            time.sleep(2)

        # Démarrer le nouveau monitor
        start_monitor()

        return jsonify({'status': 'success', 'redirect': '/'})

    except Exception as e:
        logger.error(f"Setup error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


if __name__ == '__main__':
    ssl_context = None
    if os.path.exists('cert.pem') and os.path.exists('key.pem'):
        ssl_context = ('cert.pem', 'key.pem')
        logger.info("HTTPS activé")
    else:
        logger.info("HTTP (pas de certificats SSL)")

    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    try:
        app.run(host='0.0.0.0', port=5000, debug=False,
                threaded=True, ssl_context=ssl_context)
    except KeyboardInterrupt:
        if monitor:
            monitor.stop()
