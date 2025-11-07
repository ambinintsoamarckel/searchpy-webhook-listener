#!/bin/bash
set -e  # Arrêt immédiat en cas d'erreur
set -u  # Erreur si variable non définie

# Ce script est exécuté DANS le conteneur "webhook-listener"
# Il manipule les chemins de l'hôte grâce aux volumes montés

SERVICE_NAME=${1:-"unknown"}
N_ATTEMPTS=${2:-0}

# --- Configuration ---
LOG_FOLDER_MOUNT="/usr/src/app/logs_host"
BACKUP_MOUNT="/usr/src/app/backups_mount"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE_NAME="${SERVICE_NAME}_${TIMESTAMP}.tar.gz"
LOG_FILE_BACKUP="${BACKUP_MOUNT}/${LOG_FILE_NAME}"

# Couleurs pour output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  PROCÉDURE DE NETTOYAGE CRITIQUE${NC}"
echo -e "${YELLOW}========================================${NC}"
echo "Service affecté : $SERVICE_NAME"
echo "Tentatives échouées : $N_ATTEMPTS"
echo "Timestamp : $TIMESTAMP"
echo ""

# --- Vérifications préalables ---
check_disk_space() {
    AVAILABLE_SPACE=$(df -BM "$BACKUP_MOUNT" | awk 'NR==2 {print $4}' | sed 's/M//')
    REQUIRED_SPACE=100  # 100MB minimum

    if [ "$AVAILABLE_SPACE" -lt "$REQUIRED_SPACE" ]; then
        echo -e "${RED}❌ ERREUR: Espace disque insuffisant${NC}"
        echo "Disponible: ${AVAILABLE_SPACE}MB, Requis: ${REQUIRED_SPACE}MB"
        exit 1
    fi
    echo -e "${GREEN}✓ Espace disque suffisant: ${AVAILABLE_SPACE}MB${NC}"
}

check_directories() {
    if [ ! -d "$BACKUP_MOUNT" ]; then
        echo -e "${RED}❌ ERREUR: Répertoire de sauvegarde inaccessible: $BACKUP_MOUNT${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Répertoire de sauvegarde accessible${NC}"

    if [ ! -d "$LOG_FOLDER_MOUNT" ]; then
        echo -e "${YELLOW}⚠️  Dossier logs inexistant, création...${NC}"
        mkdir -p "$LOG_FOLDER_MOUNT"
    fi
    echo -e "${GREEN}✓ Dossier logs accessible${NC}"
}

# --- Exécution des vérifications ---
echo "1. Vérifications préalables..."
check_disk_space
check_directories
echo ""

# --- Sauvegarde des logs ---
echo "2. Sauvegarde des logs..."

# Vérifier si le dossier logs contient des fichiers
if [ -d "$LOG_FOLDER_MOUNT" ] && [ "$(ls -A $LOG_FOLDER_MOUNT 2>/dev/null)" ]; then

    echo "   → Compression des logs en cours..."

    # Créer l'archive tar.gz
    if tar -czf "$LOG_FILE_BACKUP" -C "$(dirname ${LOG_FOLDER_MOUNT})" "$(basename ${LOG_FOLDER_MOUNT})" 2>/dev/null; then

        # Vérifier que l'archive a été créée et n'est pas vide
        if [ -f "$LOG_FILE_BACKUP" ] && [ -s "$LOG_FILE_BACKUP" ]; then
            ARCHIVE_SIZE=$(du -h "$LOG_FILE_BACKUP" | cut -f1)
            echo -e "${GREEN}✅ Logs sauvegardés: $LOG_FILE_BACKUP ($ARCHIVE_SIZE)${NC}"
        else
            echo -e "${RED}❌ Erreur: Archive créée mais vide ou corrompue${NC}"
            exit 1
        fi
    else
        echo -e "${RED}❌ Erreur lors de la compression des logs${NC}"
        exit 1
    fi

    # Renommer l'ancien dossier (sauvegarde de sécurité locale)
    OLD_FOLDER_NAME="${LOG_FOLDER_MOUNT}_old_${TIMESTAMP}"
    if mv "$LOG_FOLDER_MOUNT" "$OLD_FOLDER_NAME" 2>/dev/null; then
        echo -e "${GREEN}✅ Ancien dossier renommé: $(basename $OLD_FOLDER_NAME)${NC}"
    else
        echo -e "${RED}❌ Erreur lors du renommage du dossier logs${NC}"
        exit 1
    fi

    # Créer un nouveau dossier logs vide
    if mkdir -p "$LOG_FOLDER_MOUNT" 2>/dev/null; then
        # Appliquer les bonnes permissions (1000:1000 = user standard)
        chown -R 1000:1000 "$LOG_FOLDER_MOUNT" 2>/dev/null || true
        chmod 777 "$LOG_FOLDER_MOUNT" 2>/dev/null || true
        echo -e "${GREEN}✅ Nouveau dossier logs créé avec permissions correctes${NC}"
    else
        echo -e "${RED}❌ Erreur lors de la création du nouveau dossier logs${NC}"
        exit 1
    fi

else
    echo -e "${YELLOW}⚠️  Dossier logs vide ou inexistant, aucune sauvegarde nécessaire${NC}"
    # S'assurer que le dossier existe quand même
    mkdir -p "$LOG_FOLDER_MOUNT" 2>/dev/null || true
    chown -R 1000:1000 "$LOG_FOLDER_MOUNT" 2>/dev/null || true
    chmod 777 "$LOG_FOLDER_MOUNT" 2>/dev/null || true
fi

echo ""

# --- Nettoyage des anciennes sauvegardes ---
echo "3. Nettoyage des anciennes sauvegardes (conservation des 10 dernières)..."

BACKUP_COUNT=$(ls -1 "$BACKUP_MOUNT"/*.tar.gz 2>/dev/null | wc -l)
if [ "$BACKUP_COUNT" -gt 10 ]; then
    echo "   → $BACKUP_COUNT sauvegardes trouvées, suppression des plus anciennes..."
    ls -1t "$BACKUP_MOUNT"/*.tar.gz | tail -n +11 | xargs rm -f 2>/dev/null || true
    echo -e "${GREEN}✅ Anciennes sauvegardes nettoyées${NC}"
else
    echo "   → $BACKUP_COUNT sauvegardes trouvées (< 10), aucun nettoyage nécessaire"
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  NETTOYAGE TERMINÉ AVEC SUCCÈS${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Résumé:"
echo "  - Archive: $LOG_FILE_NAME"
echo "  - Localisation: $BACKUP_MOUNT"
echo "  - Service: $SERVICE_NAME"
echo "  - Tentatives: $N_ATTEMPTS"
echo ""

exit 0
