import os
import time
import json
import subprocess
import requests
import logging
import colorlog
from flask import Flask, request
from pathlib import Path
import hmac
import threading

# --- Logger Setup ---
def setup_logger():
    """Configure un logger coloré."""
    logger = logging.getLogger(__name__)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = colorlog.StreamHandler()
        formatter = colorlog.ColoredFormatter(
            '%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(message)s',
            log_colors={
                'DEBUG':    'cyan',
                'INFO':     'green',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'red,bg_white',
            }
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

logger = setup_logger()

# --- Configuration ---
CRITICAL_SERVICE_NAME = os.environ.get("CRITICAL_SERVICE_NAME", "searchpy-app-prod")
CRITICAL_FAIL_COUNT = int(os.environ.get("CRITICAL_FAIL_COUNT", 3))
COMPOSE_FILE_PATH = os.environ.get("COMPOSE_FILE_PATH", "/host/docker-compose.yml")
WEBHOOK_URL_CRITICAL = os.environ.get("WEBHOOK_URL_CRITICAL", "")
WEBHOOK_URL_FINAL = os.environ.get("WEBHOOK_URL_FINAL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
STATE_FILE = "/usr/src/app/state/listener_state.json"
RECOVERY_SCRIPT_PATH = "/usr/src/app/critical_recovery.sh"
RESOLUTION_TIMEOUT = int(os.environ.get("RESOLUTION_TIMEOUT", 300))  # 5 min par défaut

app = Flask(__name__)
# Désactiver les logs de Flask pour ne garder que les nôtres
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)


# --- Persistence Layer ---

class StateManager:
    """Gère l'état persistant sur disque"""

    def __init__(self, state_file):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self.load_state()

    def load_state(self):
        """Charge l'état depuis le fichier JSON"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except FileNotFoundError:
                logger.warning(f"Fichier d'état non trouvé: {self.state_file}, création d'un nouveau.")
            except json.JSONDecodeError:
                logger.warning("État corrompu, réinitialisation")
            except IOError as e:
                logger.error(f"Erreur de lecture du fichier d'état: {e}, réinitialisation.")
        return {
            "fail_count": {},
            "last_message_time": {},
            "service_status": {},  # "NORMAL", "SURVEILLANCE_POST_RESTART", "PAUSED"
            "paused_services": {},
            "recovery_history": [],
            "warning_sent": {}  # Pour savoir si on a déjà envoyé le warning initial
        }

    def save_state(self):
        """Sauvegarde l'état sur disque"""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Erreur sauvegarde état: {e}")

    def increment_fail_count(self, service_name):
        """Incrémente le compteur d'échecs"""
        current_count = self.state["fail_count"].get(service_name, 0) + 1
        self.state["fail_count"][service_name] = current_count
        self.state["last_message_time"][service_name] = time.time()
        self.save_state()
        return current_count

    def reset_fail_count(self, service_name):
        """Réinitialise le compteur"""
        self.state["fail_count"][service_name] = 0
        self.state["warning_sent"][service_name] = False
        self.save_state()

    def get_service_status(self, service_name):
        """Retourne le statut du service"""
        return self.state["service_status"].get(service_name, "NORMAL")

    def set_service_status(self, service_name, status):
        """Change le statut du service"""
        self.state["service_status"][service_name] = status
        self.save_state()
        logger.info(f"📊 Service {service_name} → Statut: {status}")

    def pause_service(self, service_name, reason):
        """Met le service en pause"""
        self.state["paused_services"][service_name] = {
            "paused_at": time.time(),
            "reason": reason,
            "last_message_time": time.time()
        }
        self.set_service_status(service_name, "PAUSED")
        logger.warning(f"⏸️ Service {service_name} mis en PAUSE: {reason}")

    def unpause_service(self, service_name):
        """Retire le service de la pause"""
        if service_name in self.state["paused_services"]:
            del self.state["paused_services"][service_name]
        self.set_service_status(service_name, "NORMAL")
        self.reset_fail_count(service_name)
        logger.info(f"▶️ Service {service_name} retiré de la pause")

    def is_paused(self, service_name):
        """Vérifie si le service est en pause"""
        return service_name in self.state["paused_services"]

    def update_last_message_time(self, service_name):
        """Met à jour le timestamp du dernier message"""
        self.state["last_message_time"][service_name] = time.time()
        if service_name in self.state["paused_services"]:
            self.state["paused_services"][service_name]["last_message_time"] = time.time()
        self.save_state()

    def get_time_since_last_message(self, service_name):
        """Retourne le temps écoulé depuis le dernier message"""
        last_time = self.state["last_message_time"].get(service_name, 0)
        return time.time() - last_time

    def has_warning_been_sent(self, service_name):
        """Vérifie si le warning initial a été envoyé"""
        return self.state["warning_sent"].get(service_name, False)

    def mark_warning_sent(self, service_name):
        """Marque le warning comme envoyé"""
        self.state["warning_sent"][service_name] = True
        self.save_state()

    def add_recovery_event(self, service_name, event_type, details):
        """Ajoute un événement dans l'historique"""
        self.state["recovery_history"].append({
            "service": service_name,
            "timestamp": time.time(),
            "event": event_type,
            "details": details
        })
        self.save_state()

state_manager = StateManager(STATE_FILE)

# --- Constantes d'Alerte ---
COLORS = {
    "info": 3447003,
    "warning": 16776960,
    "critical": 15158332,
    "success": 5763719,
    "FINAL_STOP": 15158332
}
EMOJIS = {
    "info": "ℹ️",
    "warning": "⚠️",
    "critical": "🚨",
    "success": "✅",
    "FINAL_STOP": "🔴"
}

# --- Fonctions d'Alerte ---

def send_discord_alert(webhook_url, message, level="info"):
    """Envoie une notification Discord avec embed formaté"""
    if not webhook_url:
        logger.warning(f"Alerte {level} non envoyée: URL manquante")
        return

    payload = {
        "embeds": [{
            "title": f"{EMOJIS.get(level, '📢')} Alerte Monitoring - {level.upper()}",
            "description": message,
            "color": COLORS.get(level, 3447003),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "footer": {"text": f"SearchPy Monitoring System - VPS {os.environ.get('HOSTNAME', 'Unknown')}"}
        }],
        "username": "SearchPy Watchdog"
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Alerte Discord envoyée ({level})")
    except requests.exceptions.RequestException as e:
        logger.error(f"Erreur envoi Discord: {e}")

# --- Fonctions Docker ---

def run_docker_compose_command(action, compose_file):
    """
    Exécute une commande docker compose sans shell=True (plus robuste et sécurisé).
    action: string ("down" ou "up -d")
    """
    command_parts = ["docker-compose", "-f", compose_file] + action.split()
    logger.info(f"🐳 Exécution sécurisée: {' '.join(command_parts)}")

    try:
        result = subprocess.run(
            command_parts,
            check=True,
            capture_output=True,
            text=True,
            timeout=120
        )
        logger.info(f"Commande réussie: {result.stdout.strip()}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout lors de l'exécution Docker Compose")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur Docker Compose (Code {e.returncode}): {e.stderr.strip()}")
        logger.error("Vérifiez la permission du socket Docker (GID) ou le chemin du compose.")
        return False
    except FileNotFoundError as e:
        logger.critical(f"Le binaire 'docker-compose' n'a pas été trouvé dans le PATH! {e}")
        return False
    except Exception as e:
        logger.error(f"Erreur inconnue lors de l'exécution de Docker Compose: {e}")
        return False

def perform_recovery(service_name, fail_count):
    """Lance la procédure de récupération complète"""
    logger.warning(f"🔧 Début de la procédure de récupération pour {service_name}")
    # Tentative de redémarrage
    if not run_docker_compose_command("down", COMPOSE_FILE_PATH):
        logger.critical(f"❌ Échec de 'docker-compose down'")
        state_manager.pause_service(service_name, "docker_down_failed")
        state_manager.add_recovery_event(service_name, "recovery_failed", "docker-compose down failed")

        send_discord_alert(
            WEBHOOK_URL_FINAL,
            f"**🔴 ÉCHEC COMMANDE DOCKER - INTERVENTION REQUISE 🔴**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Problème**: La commande `docker-compose down` a échoué\n"
            f"**Action**: Service mis en PAUSE - Écoute arrêtée\n\n"
            f"@everyone - Intervention manuelle nécessaire",
            level="FINAL_STOP"
        )
        return False

    time.sleep(5)

    if not run_docker_compose_command("up -d", COMPOSE_FILE_PATH):
        logger.critical(f"❌ Échec de 'docker-compose up -d'")
        state_manager.pause_service(service_name, "docker_up_failed")
        state_manager.add_recovery_event(service_name, "recovery_failed", "docker-compose up failed")

        send_discord_alert(
            WEBHOOK_URL_FINAL,
            f"**🔴 ÉCHEC COMMANDE DOCKER - INTERVENTION REQUISE 🔴**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Problème**: La commande `docker-compose up -d` a échoué\n"
            f"**Action**: Service mis en PAUSE - Écoute arrêtée\n\n"
            f"@everyone - Intervention manuelle nécessaire",
            level="FINAL_STOP"
        )
        return False

    # Redémarrage réussi, passage en mode surveillance
    logger.info(f"✅ Redémarrage Docker réussi, passage en mode SURVEILLANCE")
    state_manager.set_service_status(service_name, "SURVEILLANCE_POST_RESTART")
    state_manager.add_recovery_event(service_name, "recovery_started", f"docker restarted, fail_count={fail_count}")

    return True

# --- Thread de surveillance ---

def monitor_paused_services():
    """Vérifie périodiquement si les services en pause peuvent être réactivés"""
    while True:
        time.sleep(30)  # Vérification toutes les 30 secondes

        for service_name in list(state_manager.state["paused_services"].keys()):
            time_since_last = state_manager.get_time_since_last_message(service_name)

            if time_since_last >= RESOLUTION_TIMEOUT:
                logger.info(f"✅ Service {service_name}: Pas de message depuis {RESOLUTION_TIMEOUT}s, considéré comme résolu")

                state_manager.add_recovery_event(service_name, "resolved_manually", f"no messages for {RESOLUTION_TIMEOUT}s")
                state_manager.unpause_service(service_name)

                send_discord_alert(
                    WEBHOOK_URL_CRITICAL,
                    f"**✅ SERVICE RÉTABLI (Intervention manuelle)**\n\n"
                    f"**Service**: `{service_name}`\n"
                    f"**Résolution**: Aucun échec détecté depuis {RESOLUTION_TIMEOUT//60} minutes\n"
                    f"**Action**: Compteur réinitialisé - Surveillance normale reprise\n\n"
                    f"Le service est maintenant stable.",
                    level="success"
                )

        # Vérifier aussi les services en surveillance post-restart
        for service_name, status in list(state_manager.state["service_status"].items()):
            if status == "SURVEILLANCE_POST_RESTART":
                time_since_last = state_manager.get_time_since_last_message(service_name)

                if time_since_last >= RESOLUTION_TIMEOUT:
                    logger.info(f"✅ Service {service_name}: Stable après redémarrage, considéré comme résolu")

                    state_manager.add_recovery_event(service_name, "resolved_automatically", f"stable for {RESOLUTION_TIMEOUT}s after restart")
                    state_manager.set_service_status(service_name, "NORMAL")
                    state_manager.reset_fail_count(service_name)

                    send_discord_alert(
                        WEBHOOK_URL_CRITICAL,
                        f"**✅ SERVICE RÉTABLI AUTOMATIQUEMENT**\n\n"
                        f"**Service**: `{service_name}`\n"
                        f"**Résolution**: Aucun échec détecté depuis {RESOLUTION_TIMEOUT//60} minutes après redémarrage\n"
                        f"**Action**: Compteur réinitialisé - Surveillance normale reprise\n\n"
                        f"La réparation automatique a réussi.",
                        level="success"
                    )

# Démarrer le thread de surveillance
monitor_thread = threading.Thread(target=monitor_paused_services, daemon=True)
monitor_thread.start()

# --- Authentification ---

def verify_webhook_token():
    """Vérifie le token d'authentification du webhook"""
    client_ip = request.remote_addr

    # Faire confiance au réseau Docker interne
    if client_ip.startswith('172.'):
        return True
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET non défini, authentification désactivée (mode développement)")
        return True

    auth_header = request.headers.get('X-Webhook-Token')
    if not auth_header:
        return False

    return hmac.compare_digest(auth_header, WEBHOOK_SECRET)

# --- Route Webhook ---

@app.route('/health', methods=['GET'])
def health_check():
    """Healthcheck du listener lui-même"""
    return {
        "status": "healthy",
        "service": "webhook-listener",
        "timestamp": time.time(),
        "state_file_exists": Path(STATE_FILE).exists()
    }, 200

@app.route('/autoheal-event', methods=['POST'])
def handle_autoheal_event():
    """Gère les événements de santé du service critique"""
    logger.info(f"📥 Requête reçue depuis: {request.remote_addr}")

    if not verify_webhook_token():
        logger.warning("Tentative d'accès non autorisée (token invalide/manquant)")
        return {"error": "Unauthorized"}, 401

    data = request.json
    if not data:
        return {"error": "Invalid JSON"}, 400

    service_name = None

    # Format autoheal : {"content": "Container searchpy-app-dev (...) found..."}
    if 'content' in data:
        import re
        match = re.search(r'Container (/?)([a-zA-Z0-9_-]+)', data['content'])
        if match:
            service_name = match.group(2)
            logger.info(f"🔍 Service extrait du content: {service_name}")

    # Format custom : {"container_name": "...", "type": "..."}
    if not service_name:
        service_name = data.get('container_name')

    if not service_name:
        logger.warning("Aucun nom de service trouvé dans la requête")
        return {"error": "No service name found"}, 400

    if service_name != CRITICAL_SERVICE_NAME:
        return {"status": "ignored", "reason": "not_critical_service"}, 200

    # Mettre à jour le timestamp du dernier message
    state_manager.update_last_message_time(service_name)

    # Vérifier si le service est en pause
    if state_manager.is_paused(service_name):
        logger.info(f"⏸️ Service {service_name} en PAUSE, événement ignoré silencieusement")
        return {"status": "paused"}, 200

    # Vérifier si on est en surveillance post-restart
    if state_manager.get_service_status(service_name) == "SURVEILLANCE_POST_RESTART":
        logger.warning(f"❌ Service {service_name} toujours unhealthy après redémarrage!")

        state_manager.pause_service(service_name, "still_unhealthy_after_restart")
        state_manager.add_recovery_event(service_name, "recovery_failed", "still unhealthy after restart")

        send_discord_alert(
            WEBHOOK_URL_FINAL,
            f"**🔴 ÉCHEC DE LA RÉPARATION AUTOMATIQUE**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Problème**: Le service est toujours unhealthy après redémarrage complet\n"
            f"**Action**: Service mis en PAUSE - Écoute arrêtée\n\n"
            f"@everyone - **INTERVENTION MANUELLE REQUISE**",
            level="FINAL_STOP"
        )

        return {"status": "paused_after_failed_recovery"}, 200

    # Incrémenter le compteur
    current_count = state_manager.increment_fail_count(service_name)
    logger.info(f"Échec détecté pour '{service_name}'. Total: {current_count}/{CRITICAL_FAIL_COUNT}")

    # Premier échec : envoyer le warning
    if current_count == 1 and not state_manager.has_warning_been_sent(service_name):
        state_manager.mark_warning_sent(service_name)
        send_discord_alert(
            WEBHOOK_URL_CRITICAL,
            f"**⚠️ SERVICE UNHEALTHY DÉTECTÉ**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Statut**: Unhealthy (1/{CRITICAL_FAIL_COUNT})\n"
            f"**Action**: Surveillance en cours\n\n"
            f"Si {CRITICAL_FAIL_COUNT} échecs consécutifs sont détectés, "
            f"une réparation automatique sera lancée (`docker-compose down/up`).",
            level="warning"
        )

    # Seuil critique atteint
    if current_count >= CRITICAL_FAIL_COUNT:
        logger.warning(f"🚨 Seuil critique atteint pour '{service_name}'. Démarrage de la remédiation.")

        send_discord_alert(
            WEBHOOK_URL_CRITICAL,
            f"**🚨 SEUIL CRITIQUE ATTEINT**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Échecs consécutifs**: {current_count}/{CRITICAL_FAIL_COUNT}\n"
            f"**Action**: Lancement de la réparation automatique\n\n"
            f"📋 Étapes:\n"
            f"1. `docker-compose down`\n"
            f"2. `docker-compose up -d`\n"
            f"3. Surveillance pendant {RESOLUTION_TIMEOUT//60} minutes",
            level="critical"
        )

        # Lancer la récupération
        if perform_recovery(service_name, current_count):
            return {"status": "recovery_initiated", "next_state": "surveillance"}, 200
        else:
            return {"status": "recovery_failed", "service_paused": True}, 500

    return {"status": "counted", "current": current_count, "threshold": CRITICAL_FAIL_COUNT}, 200

@app.route('/reset', methods=['POST'])
def reset_state():
    """Endpoint pour réinitialiser l'état (admin only)"""
    if not verify_webhook_token():
        logger.warning("Tentative d'accès non autorisée sur /reset")
        return {"error": "Unauthorized"}, 401

    service_name = request.json.get('service_name', CRITICAL_SERVICE_NAME)

    # Retirer de la pause si nécessaire
    if state_manager.is_paused(service_name):
        state_manager.unpause_service(service_name)
    else:
        state_manager.reset_fail_count(service_name)
        state_manager.set_service_status(service_name, "NORMAL")

    logger.info(f"État réinitialisé manuellement pour le service '{service_name}'")
    return {"status": "reset", "service": service_name}, 200

@app.route('/status', methods=['GET'])
def get_status():
    """Retourne l'état actuel du système"""
    return {
        "state": state_manager.state,
        "critical_service": CRITICAL_SERVICE_NAME,
        "threshold": CRITICAL_FAIL_COUNT,
        "resolution_timeout": RESOLUTION_TIMEOUT
    }, 200

if __name__ == '__main__':
    logger.info("🚀 Démarrage du Webhook Listener Amélioré v2")
    logger.info(f"Service critique à surveiller: {CRITICAL_SERVICE_NAME}")
    logger.info(f"Seuil d'échecs avant action: {CRITICAL_FAIL_COUNT}")
    logger.info(f"Délai de résolution: {RESOLUTION_TIMEOUT}s ({RESOLUTION_TIMEOUT//60} min)")
    logger.info(f"Authentification: {'Activée' if WEBHOOK_SECRET else 'DÉSACTIVÉE (MODE DÉVELOPPEMENT)'}")
    logger.info(f"Thread de surveillance: Démarré")
    app.run(host='0.0.0.0', port=5000)
