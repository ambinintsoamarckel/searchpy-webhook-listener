# ========================================
# Webhook Listener - Production Dockerfile
# Pour publication sur Docker Hub / GitHub Registry
# ========================================

FROM python:3.11-slim as base

# Métadonnées
LABEL maintainer="marsonambinintsoa@gmail.com"
LABEL org.opencontainers.image.source="https://github.com/ambinintsoamarckel/searchpy-webhook-listener/"
LABEL org.opencontainers.image.description="Intelligent auto-healing webhook listener for Docker containers"


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

# Installation de Docker Compose v2 (standalone)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        && \
    # Télécharger Docker Compose v2
    DOCKER_COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep 'tag_name' | cut -d'"' -f4) && \
    curl -L "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose && \
    chmod +x /usr/local/bin/docker-compose && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Création des répertoires nécessaires
RUN mkdir -p \
    /usr/src/app/state


WORKDIR /usr/src/app

# Copier les dépendances Python depuis le stage précédent
COPY --from=dependencies /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=dependencies /usr/local/bin /usr/local/bin

# Copier le code source
COPY src/webhook_listener.py .

# ⚠️ EXÉCUTION EN ROOT pour accéder au socket Docker
# (Pas de USER webhookuser ici)

# Healthcheck intégré
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Exposition du port
EXPOSE 5000

# Point d'entrée
CMD ["python", "webhook_listener.py"]
