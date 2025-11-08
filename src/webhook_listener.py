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
COOLDOWN_PERIOD = int(os.environ.get("COOLDOWN_PERIOD", 300))  # 5 min par défaut

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
            "last_attempt_time": {},
            "critical_recovery_triggered": {},
            "recovery_history": []
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
        self.state["last_attempt_time"][service_name] = time.time()
        self.save_state()
        return current_count

    def reset_fail_count(self, service_name):
        """Réinitialise le compteur"""
        self.state["fail_count"][service_name] = 0
        self.state["critical_recovery_triggered"][service_name] = False
        self.save_state()

    def is_in_cooldown(self, service_name):
        """Vérifie si on est en période de cooldown"""
        last_attempt = self.state["last_attempt_time"].get(service_name, 0)
        return (time.time() - last_attempt) < COOLDOWN_PERIOD

    def mark_recovery_triggered(self, service_name):
        """Marque qu'une remédiation critique a été déclenchée"""
        self.state["critical_recovery_triggered"][service_name] = True
        self.state["recovery_history"].append({
            "service": service_name,
            "timestamp": time.time(),
            "fail_count": self.state["fail_count"].get(service_name, 0)
        })
        self.save_state()

    def is_recovery_triggered(self, service_name):
        """Vérifie si une remédiation est en cours"""
        return self.state["critical_recovery_triggered"].get(service_name, False)

state_manager = StateManager(STATE_FILE)

# --- Constantes d'Alerte ---
COLORS = {"info": 3447003, "warning": 16776960, "critical": 15158332, "FINAL_STOP": 15158332}
EMOJIS = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨", "FINAL_STOP": "🔴"}

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

# --- Fonctions Docker (VERSION FINALE ET SÉCURISÉE) ---

def run_docker_compose_command(action, compose_file):
    """
    Exécute une commande docker compose sans shell=True (plus robuste et sécurisé).
    action: string ("down" ou "up -d")
    """
    # Construction de la commande en liste, y compris la séparation de "up -d"
    command_parts = ["docker-compose", "-f", compose_file] + action.split()

    logger.info(f"🐳 Exécution sécurisée: {' '.join(command_parts)}")

    try:
        # shell=True est retiré.
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

def run_critical_recovery_script(service_name, attempt_count):
    """Exécute le script de sauvegarde des logs"""
    logger.info(f"📦 Exécution du script de remédiation: {RECOVERY_SCRIPT_PATH}")
    try:
        result = subprocess.run(
            [RECOVERY_SCRIPT_PATH, service_name, str(attempt_count)],
            check=True, capture_output=True, text=True, timeout=60
        )
        logger.info(f"Script de remédiation réussi: {result.stdout.strip()}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout du script de remédiation")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Erreur script: {e.stderr.strip()}")
        return False

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
        # 🔍 AJOUTE CE DEBUG AU DÉBUT
    logger.info(f"📥 Requête reçue depuis: {request.remote_addr}")
    logger.info(f"📋 Headers complets: {dict(request.headers)}")
    logger.info(f"📦 Body: {request.get_data(as_text=True)}")
    logger.info(f"🔑 X-Webhook-Token trouvé: {request.headers.get('X-Webhook-Token')}")
    logger.info(f"🔒 WEBHOOK_SECRET attendu: {WEBHOOK_SECRET[:10]}...")
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
            service_name = match.group(2)  # Extrait "searchpy-app-dev"
            logger.info(f"🔍 Service extrait du content: {service_name}")

    # Format custom : {"container_name": "...", "type": "..."}
    if not service_name:
        service_name = data.get('container_name')

    if not service_name:
        logger.warning("Aucun nom de service trouvé dans la requête")
        return {"error": "No service name found"}, 400

    if not service_name or service_name != CRITICAL_SERVICE_NAME:
        return {"status": "ignored", "reason": "not_critical_service"}, 200


    if state_manager.is_in_cooldown(service_name):
        logger.info(f"Service {service_name} en cooldown, événement ignoré")
        return {"status": "cooldown"}, 200

    current_count = state_manager.increment_fail_count(service_name)
    logger.info(f"Échec détecté pour '{service_name}'. Total: {current_count}/{CRITICAL_FAIL_COUNT}")

    if current_count >= CRITICAL_FAIL_COUNT and not state_manager.is_recovery_triggered(service_name):
        logger.warning(f"Seuil critique atteint pour '{service_name}'. Démarrage de la remédiation.")
        send_discord_alert(
            WEBHOOK_URL_CRITICAL,
            f"**Service**: `{service_name}`\n"
            f"**Échecs consécutifs**: {current_count}\n"
            f"**Action**: Démarrage de la remédiation critique\n"
            f"- Sauvegarde des logs\n"
            f"- Redémarrage complet de la stack",
            level="critical"
        )
        state_manager.mark_recovery_triggered(service_name)


        if run_docker_compose_command("down", COMPOSE_FILE_PATH):
            time.sleep(5)
            if run_docker_compose_command("up -d", COMPOSE_FILE_PATH):
                logger.info("Stack relancée avec succès, attente du prochain healthcheck.")
                state_manager.reset_fail_count(service_name)
                return {"status": "recovery_success"}, 200

        logger.critical(f"La remédiation automatique a échoué pour '{service_name}'. Intervention manuelle requise.")
        send_discord_alert(
            WEBHOOK_URL_FINAL,
            f"**🔴 ARRÊT FINAL - INTERVENTION REQUISE 🔴**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Problème**: La remédiation automatique (docker compose down/up) a échoué.\n\n"
            f"@everyone - Panne critique détectée",
            level="FINAL_STOP"
        )
        return {"status": "error", "message": "Full recovery failed"}, 500

    return {"status": "counted", "current": current_count, "threshold": CRITICAL_FAIL_COUNT}, 200

@app.route('/reset', methods=['POST'])
def reset_state():
    """Endpoint pour réinitialiser l'état (admin only)"""
    if not verify_webhook_token():
        logger.warning("Tentative d'accès non autorisée sur /reset")
        return {"error": "Unauthorized"}, 401

    service_name = request.json.get('service_name', CRITICAL_SERVICE_NAME)
    state_manager.reset_fail_count(service_name)
    logger.info(f"État réinitialisé manuellement pour le service '{service_name}'")
    return {"status": "reset", "service": service_name}, 200

@app.route('/status', methods=['GET'])
def get_status():
    """Retourne l'état actuel du système"""
    return {
        "state": state_manager.state,
        "critical_service": CRITICAL_SERVICE_NAME,
        "threshold": CRITICAL_FAIL_COUNT,
        "cooldown_period": COOLDOWN_PERIOD
    }, 200

if __name__ == '__main__':
    logger.info("🚀 Démarrage du Webhook Listener Sécurisé")
    logger.info(f"Service critique à surveiller: {CRITICAL_SERVICE_NAME}")
    logger.info(f"Seuil d'échecs avant action: {CRITICAL_FAIL_COUNT}")
    logger.info(f"Période de cooldown: {COOLDOWN_PERIOD}s")
    logger.info(f"Authentification: {'Activée' if WEBHOOK_SECRET else 'DÉSACTIVÉE (MODE DÉVELOPPEMENT)'}")
    app.run(host='0.0.0.0', port=5000)
