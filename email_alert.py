#!/usr/bin/env python3
"""
BL-FMO-LITE — email_alert.py
Gestion des alertes email pour le monitoring FM.
"""

import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class EmailAlert:
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            config = json.load(f)

        self.config = config['email']
        self.station_name = config['station']['name']
        self.frequency = config['station']['frequency_display']
        self.last_alert_time = None
        self.cooldown = timedelta(minutes=self.config.get('cooldown_minutes', 1))

    def can_send_alert(self):
        if not self.config['enabled']:
            return False
        if self.last_alert_time is None:
            return True
        return datetime.now() - self.last_alert_time > self.cooldown

    def send_alert(self, alert_type, details="", skip_cooldown=False):
        if "rétabli" in alert_type.lower() or skip_cooldown:
            logger.info(f"Envoi alerte '{alert_type}' (cooldown ignoré)")
        elif not self.can_send_alert():
            logger.info("Alerte non envoyée (cooldown actif)")
            return False

        try:
            msg = MIMEMultipart('alternative')
            if "rétabli" in alert_type.lower():
                msg['Subject'] = f"✅ RÉTABLI - {self.station_name} - {alert_type}"
            else:
                msg['Subject'] = f"⚠️ ALERTE - {self.station_name} - {alert_type}"
            msg['From'] = self.config['sender_email']
            msg['To'] = ', '.join(self.config['recipient_emails'])

            timestamp = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
            is_ok = "rétabli" in alert_type.lower()

            text_content = f"""
ALERTE DE SURVEILLANCE FM
========================

Station: {self.station_name}
Fréquence: {self.frequency}
Type d'alerte: {alert_type}
Date et heure: {timestamp}

Détails:
{details}

---
BL-FMO-LITE — Système de surveillance FM
            """

            html_content = f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; }}
                    .alert-box {{
                        background-color: {"#d4edda" if is_ok else "#fff3cd"};
                        border-left: 4px solid {"#28a745" if is_ok else "#ffc107"};
                        padding: 20px; margin: 20px 0;
                    }}
                    .header {{ color: {"#155724" if is_ok else "#856404"}; font-size: 24px; font-weight: bold; }}
                    .info {{ margin: 10px 0; }}
                    .label {{ font-weight: bold; color: #333; }}
                    .footer {{ margin-top: 20px; color: #666; font-size: 12px; }}
                </style>
            </head>
            <body>
                <div class="alert-box">
                    <div class="header">{"✅" if is_ok else "⚠️"} {alert_type.upper()}</div>
                    <hr>
                    <div class="info"><span class="label">Station:</span> {self.station_name}</div>
                    <div class="info"><span class="label">Fréquence:</span> {self.frequency}</div>
                    <div class="info"><span class="label">Type d'alerte:</span> {alert_type}</div>
                    <div class="info"><span class="label">Date et heure:</span> {timestamp}</div>
                    <hr>
                    <div class="info"><span class="label">Détails:</span><br>{details}</div>
                    <div class="footer">BL-FMO-LITE — Système de surveillance FM</div>
                </div>
            </body>
            </html>
            """

            msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
            msg.attach(MIMEText(html_content, 'html', 'utf-8'))

            # Marquer AVANT l'envoi pour éviter la boucle infinie sur erreur SMTP
            self.last_alert_time = datetime.now()

            with smtplib.SMTP(self.config['smtp_server'], self.config['smtp_port']) as server:
                if self.config['use_tls']:
                    server.starttls()
                server.login(
                    self.config['sender_email'],
                    self.config['sender_password'].replace(' ', '')
                )
                server.sendmail(
                    self.config['sender_email'],
                    self.config['recipient_emails'],
                    msg.as_string()
                )

            logger.info(f"Alerte email envoyée: {alert_type}")
            return True

        except Exception as e:
            logger.error(f"Erreur envoi email: {e}")
            return False

    def send_recovery_alert(self):
        return self.send_alert(
            alert_type="Signal FM rétabli",
            details="Le signal FM a été rétabli avec succès.",
            skip_cooldown=True
        )
