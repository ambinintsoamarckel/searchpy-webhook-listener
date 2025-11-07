# ========================================
# Webhook Listener - Production Dockerfile
# Pour publication sur Docker Hub / GitHub Registry
# ========================================

FROM python:3.11-slim as base

# Métadonnées
LABEL maintainer="votre-email@example.com"
LABEL org.opencontainers.image.source="https://github.com/votre-user/searchpy-webhook-listener"
LABEL org.opencontainers.image.description="Intelligent auto-healing webhook listener for Docker containers"
LABEL org.opencontainers.image.licenses="MIT"

# Variables d'environnement
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ========================================
# Stage 1: Dependencies
# ========================================
FROM base as dependencies

WORKDIR /tmp

# Copier et installer les dépendances
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ========================================
# Stage 2: Runtime
# ========================================
FROM base as runtime

# Installation des outils système (minimaux)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        docker.io \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Création de l'utilisateur non-root
RUN groupadd -r webhookuser && \
    useradd -r -g webhookuser -u 1000 webhookuser

# Création des répertoires nécessaires
RUN mkdir -p \
    /usr/src/app/state \
    /usr/src/app/logs_host \
    /usr/src/app/backups_mount \
    && chown -R webhookuser:webhookuser /usr/src/app

WORKDIR /usr/src/app

# Copier les dépendances Python depuis le stage précédent
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=dependencies /usr/local/bin /usr/local/bin

# Copier le code source
# IMPORTANT: Le script critical_recovery.sh est maintenant DANS l'image
COPY --chown=webhookuser:webhookuser src/webhook_listener.py .
COPY --chown=webhookuser:webhookuser src/critical_recovery.sh .

# Rendre le script exécutable
RUN chmod +x critical_recovery.sh

# Changement vers l'utilisateur non-root
USER webhookuser

# Healthcheck intégré
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Exposition du port
EXPOSE 5000

# Point d'entrée
CMD ["python", "webhook_listener.py"]
