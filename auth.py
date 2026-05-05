"""
BL-FMO-LITE — auth.py
Authentification Bcrypt. Identique à BL-FMO, aucun changement nécessaire.
"""
from functools import wraps
from flask import session, redirect, url_for, jsonify, request
from flask_bcrypt import Bcrypt
import json
import logging

logger = logging.getLogger(__name__)
bcrypt = Bcrypt()

class Auth:
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.users = self.load_users()

    def load_users(self):
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
            auth_config = config.get('auth', {})
            if 'username' in auth_config and 'password_hash' in auth_config:
                return {auth_config['username']: auth_config['password_hash']}
            return self.create_default_user()
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Erreur chargement auth: {e}")
            return self.create_default_user()

    def create_default_user(self):
        logger.warning("Création utilisateur par défaut: admin/password — CHANGEZ-LE !")
        default_hash = bcrypt.generate_password_hash('password').decode('utf-8')
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
        except Exception:
            config = {}
        config['auth'] = {'username': 'admin', 'password_hash': default_hash}
        with open(self.config_path, 'w') as f:
            json.dump(config, f, indent=2)
        return {'admin': default_hash}

    def verify_credentials(self, username, password):
        if not username or not password:
            return False
        self.users = self.load_users()
        if username in self.users:
            try:
                return bcrypt.check_password_hash(self.users[username], password)
            except Exception as e:
                logger.error(f"Erreur vérification mot de passe: {e}")
        return False

    def login_required(self, f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'logged_in' not in session:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'status': 'error', 'message': 'Authentication required'}), 401
                return redirect(url_for('login', next=request.url))
            return f(*args, **kwargs)
        return decorated_function

    @staticmethod
    def hash_password(password):
        return bcrypt.generate_password_hash(password).decode('utf-8')

    @staticmethod
    def check_password(password, password_hash):
        try:
            return bcrypt.check_password_hash(password_hash, password)
        except Exception as e:
            logger.error(f"Erreur vérification: {e}")
            return False
