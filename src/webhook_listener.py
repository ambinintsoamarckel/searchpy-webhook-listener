import os
import time
import json
import hmac
import subprocess
import requests
from flask import Flask, request
from pathlib import Path

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
            except json.JSONDecodeError:
                print("⚠️ État corrompu, réinitialisation")
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
            print(f"❌ Erreur sauvegarde état: {e}")

    def increment_fail_count(self, service_name):
        """Incrémente le compteur d'échecs"""
        self.state["fail_count"][service_name] = self.state["fail_count"].get(service_name, 0) + 1
        self.state["last_attempt_time"][service_name] = time.time()
        self.save_state()
        return self.state["fail_count"][service_name]

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

# --- Fonctions d'Alerte ---

def send_discord_alert(webhook_url, message, level="info"):
    """Envoie une notification Discord avec embed formaté"""
    if not webhook_url:
        print(f"⚠️ Alerte {level} non envoyée: URL manquante")
        return

    # Couleurs selon le niveau
    colors = {
        "info": 3447003,      # Bleu
        "warning": 16776960,  # Jaune
        "critical": 15158332, # Orange
        "FINAL_STOP": 15158332  # Rouge
    }

    # Émojis selon le niveau
    emojis = {
        "info": "ℹ️",
        "warning": "⚠️",
        "critical": "🚨",
        "FINAL_STOP": "🔴"
    }

    payload = {
        "embeds": [{
            "title": f"{emojis.get(level, '📢')} Alerte Monitoring - {level.upper()}",
            "description": message,
            "color": colors.get(level, 3447003),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "footer": {
                "text": f"SearchPy Monitoring System - VPS {os.environ.get('HOSTNAME', 'Unknown')}"
            }
        }],
        "username": "SearchPy Watchdog"
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✅ Alerte Discord envoyée ({level})")
    except requests.exceptions.RequestException as e:
        print(f"❌ Erreur envoi Discord: {e}")

# --- Fonctions Docker ---

def run_docker_compose_command(command, compose_file):
    """Exécute une commande docker compose"""
    full_command = f"docker compose -f {compose_file} {command}"
    print(f"🐳 Exécution: {full_command}")
    try:
        result = subprocess.run(
            full_command,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            timeout=120  # Timeout de 2 minutes
        )
        print(f"✅ Commande réussie: {result.stdout}")
        return True
    except subprocess.TimeoutExpired:
        print(f"⏱️ Timeout lors de l'exécution Docker Compose")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ Erreur Docker Compose: {e.stderr}")
        return False
    except Exception as e:
        print(f"❌ Erreur inconnue: {e}")
        return False

def run_critical_recovery_script(service_name, attempt_count):
    """Exécute le script de sauvegarde des logs"""
    print(f"📦 Exécution du script de remédiation: {RECOVERY_SCRIPT_PATH}")
    try:
        result = subprocess.run(
            [RECOVERY_SCRIPT_PATH, service_name, str(attempt_count)],
            check=True,
            capture_output=True,
            text=True,
            timeout=60
        )
        print(f"✅ Script de remédiation réussi: {result.stdout}")
        return True
    except subprocess.TimeoutExpired:
        print(f"⏱️ Timeout du script de remédiation")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ Erreur script: {e.stderr}")
        return False

# --- Authentification ---

def verify_webhook_token():
    """Vérifie le token d'authentification du webhook"""
    if not WEBHOOK_SECRET:
        print("⚠️ WEBHOOK_SECRET non défini, mode développement")
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

    # Authentification
    if not verify_webhook_token():
        print("🔒 Tentative d'accès non autorisée")
        return {"error": "Unauthorized"}, 401

    data = request.json
    if not data:
        return {"error": "Invalid JSON"}, 400

    service_name = data.get('container_name')
    event_type = data.get('type', 'restart_attempt')

    # Filtrage du service
    if not service_name or service_name != CRITICAL_SERVICE_NAME:
        return {"status": "ignored", "reason": "not_critical_service"}, 200

    if event_type != 'restart_attempt':
        return {"status": "ignored", "reason": "not_restart_event"}, 200

    # Cooldown check
    if state_manager.is_in_cooldown(service_name):
        print(f"⏳ Service {service_name} en cooldown, événement ignoré")
        return {"status": "cooldown"}, 200

    # Incrément du compteur
    current_count = state_manager.increment_fail_count(service_name)
    print(f"📊 {service_name}: {current_count}/{CRITICAL_FAIL_COUNT} échecs")

    # Seuil atteint ?
    if current_count >= CRITICAL_FAIL_COUNT and not state_manager.is_recovery_triggered(service_name):

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

        # 1. Sauvegarde des logs
        if not run_critical_recovery_script(service_name, current_count):
            send_discord_alert(
                WEBHOOK_URL_FINAL,
                f"**🚨 ÉCHEC CRITIQUE 🚨**\n\n"
                f"**Service**: `{service_name}`\n"
                f"**Problème**: Échec de la sauvegarde des logs\n"
                f"**Action requise**: Intervention manuelle immédiate\n\n"
                f"Vérifiez les permissions et l'espace disque.",
                level="FINAL_STOP"
            )
            return {"status": "error", "message": "Log backup failed"}, 500

        # 2. Redémarrage complet
        if run_docker_compose_command("down", COMPOSE_FILE_PATH):
            time.sleep(5)  # Pause pour s'assurer que tout est bien arrêté

            if run_docker_compose_command("up -d", COMPOSE_FILE_PATH):
                state_manager.reset_fail_count(service_name)
                print("✅ Stack relancée avec succès, attente healthcheck...")
                return {"status": "recovery_success"}, 200

        # 3. Échec de la remédiation
        send_discord_alert(
            WEBHOOK_URL_FINAL,
            f"**🔴 ARRÊT FINAL - INTERVENTION REQUISE 🔴**\n\n"
            f"**Service**: `{service_name}`\n"
            f"**Tentatives échouées**: {current_count}\n"
            f"**Problème**: La remédiation automatique a échoué\n\n"
            f"**Actions à effectuer**:\n"
            f"1. Vérifier les logs système\n"
            f"2. Analyser l'état Docker\n"
            f"3. Relancer manuellement si nécessaire\n\n"
            f"@everyone - Panne critique détectée",
            level="FINAL_STOP"
        )
        return {"status": "error", "message": "Recovery failed"}, 500

    return {"status": "counted", "current": current_count, "threshold": CRITICAL_FAIL_COUNT}, 200

@app.route('/reset', methods=['POST'])
def reset_state():
    """Endpoint pour réinitialiser l'état (admin only)"""
    if not verify_webhook_token():
        return {"error": "Unauthorized"}, 401

    service_name = request.json.get('service_name', CRITICAL_SERVICE_NAME)
    state_manager.reset_fail_count(service_name)

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
    print("🚀 Démarrage du Webhook Listener Sécurisé")
    print(f"📌 Service critique: {CRITICAL_SERVICE_NAME}")
    print(f"📊 Seuil: {CRITICAL_FAIL_COUNT} tentatives")
    print(f"⏱️ Cooldown: {COOLDOWN_PERIOD}s")
    print(f"🔒 Authentification: {'Activée' if WEBHOOK_SECRET else 'DÉSACTIVÉE (DEV ONLY)'}")

    app.run(host='0.0.0.0', port=5000)
