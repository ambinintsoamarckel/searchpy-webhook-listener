# 🔧 SearchPy Webhook Listener

Intelligent auto-healing webhook listener for Docker containers with critical failure remediation.

## 🎯 Features

- ✅ **Automatic Failure Detection** - Monitors Docker container health via webhooks
- 🔄 **Progressive Remediation** - Multi-level healing strategy (restart → full reset → stop)
- 💾 **Persistent State** - Keeps track of failures across restarts
- 🔒 **Secure** - HMAC authentication on webhooks
- 📦 **Log Archiving** - Automatic backup before critical actions
- ⏱️ **Cooldown Protection** - Prevents infinite loops
- 🚨 **Discord Alerts** - Beautiful embedded notifications
- 📊 **Monitoring APIs** - `/health` and `/status` endpoints

## 🏗️ Architecture

```
┌─────────────────┐     Webhook      ┌────────────────────┐
│  Autoheal       │ ───────────────> │ Webhook Listener   │
│  (monim1)       │  Health Events   │ (This Project)     │
└─────────────────┘                  └────────────────────┘
        │                                      │
        │ Monitors                             │ Controls
        ▼                                      ▼
┌─────────────────┐                  ┌────────────────────┐
│  Your App       │                  │  Docker Compose    │
│  (Container)    │                  │  (down/up)         │
└─────────────────┘                  └────────────────────┘
```

## 🚀 Quick Start

### Docker Compose (Recommended)

```yaml
version: '3.8'

services:
  webhook-listener:
    image: votre-user/searchpy-webhook-listener:latest
    container_name: webhook-listener
    restart: always
    ports:
      - "5000:5000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./logs:/usr/src/app/logs_host
      - /var/backups/logs:/usr/src/app/backups_mount
      - ./docker-compose.yml:/host/docker-compose.yml:ro
      - webhook_state:/usr/src/app/state
    environment:
      CRITICAL_SERVICE_NAME: "your-app-prod"
      CRITICAL_FAIL_COUNT: 3
      COMPOSE_FILE_PATH: "/host/docker-compose.yml"
      WEBHOOK_URL_CRITICAL: "https://discord.com/api/webhooks/..."
      WEBHOOK_URL_FINAL: "https://discord.com/api/webhooks/..."
      WEBHOOK_SECRET: "your-secret-token-here"
      COOLDOWN_PERIOD: "300"

volumes:
  webhook_state:
```

### Docker Run

```bash
docker run -d \
  --name webhook-listener \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ./logs:/usr/src/app/logs_host \
  -v /var/backups/logs:/usr/src/app/backups_mount \
  -v ./docker-compose.yml:/host/docker-compose.yml:ro \
  -e CRITICAL_SERVICE_NAME="your-app" \
  -e CRITICAL_FAIL_COUNT=3 \
  -e WEBHOOK_SECRET="your-secret" \
  votre-user/searchpy-webhook-listener:latest
```

## 📋 Environment Variables

| Variable                | Description                          | Default                    | Required |
| ----------------------- | ------------------------------------ | -------------------------- | -------- |
| `CRITICAL_SERVICE_NAME` | Container name to monitor            | -                          | ✅        |
| `CRITICAL_FAIL_COUNT`   | Failures before critical remediation | `3`                        | ❌        |
| `COMPOSE_FILE_PATH`     | Path to docker-compose.yml           | `/host/docker-compose.yml` | ✅        |
| `WEBHOOK_URL_CRITICAL`  | Discord webhook for critical alerts  | -                          | ✅        |
| `WEBHOOK_URL_FINAL`     | Discord webhook for final stop       | -                          | ✅        |
| `WEBHOOK_SECRET`        | Authentication token                 | -                          | ✅        |
| `COOLDOWN_PERIOD`       | Seconds between remediations         | `300`                      | ❌        |
| `HOSTNAME`              | VPS identifier for alerts            | `hostname`                 | ❌        |

## 🔐 Security

### Generate Webhook Secret

```bash
openssl rand -hex 32
```

### Configure Autoheal

```yaml
autoheal:
  image: monim1/autoheal:latest
  environment:
    WEBHOOK_URL: "http://webhook-listener:5000/autoheal-event"
    WEBHOOK_HEADERS: "X-Webhook-Token: your-secret-token"
```

## 📊 API Endpoints

### Health Check
```bash
GET /health
Response: {"status":"healthy","service":"webhook-listener","timestamp":...}
```

### System Status
```bash
GET /status
Response: {
  "state": {...},
  "critical_service": "your-app",
  "threshold": 3,
  "cooldown_period": 300
}
```

### Reset State (Admin)
```bash
POST /reset
Headers: X-Webhook-Token: your-secret
Body: {"service_name": "your-app"}
```

## 🔄 Remediation Flow

```
┌─────────────────────────────────────────────────┐
│  1. Failure Detected                             │
│     └─> Autoheal restarts container              │
│         └─> Webhook → Listener counts failure    │
└─────────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────┐
│  2. Count < Threshold                            │
│     └─> Keep counting, continue monitoring       │
└─────────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────┐
│  3. Count ≥ Threshold (e.g., 3 failures)        │
│     ├─> 🚨 Send critical alert                   │
│     ├─> 📦 Backup logs (tar.gz)                  │
│     ├─> 🔄 docker compose down + up              │
│     └─> ⏱️ Start cooldown period                 │
└─────────────────────────────────────────────────┘
                     ↓
┌─────────────────────────────────────────────────┐
│  4A. Success → Reset counter                     │
│  4B. Failure → 🔴 FINAL STOP alert              │
│      └─> Manual intervention required            │
└─────────────────────────────────────────────────┘
```

## 🎨 Discord Notifications

The listener sends beautiful embedded Discord messages:

**Critical Alert** (Orange)
```
🚨 Alerte Monitoring - CRITICAL

Service: your-app-prod
Échecs consécutifs: 3
Action: Démarrage de la remédiation critique
- Sauvegarde des logs
- Redémarrage complet de la stack
```

**Final Alert** (Red)
```
🔴 ARRÊT FINAL - INTERVENTION REQUISE 🔴

Service: your-app-prod
Tentatives échouées: 3
Problème: La remédiation automatique a échoué

@everyone - Panne critique détectée
```

## 🛠️ Development

### Build Locally

```bash
git clone https://github.com/votre-user/searchpy-webhook-listener.git
cd searchpy-webhook-listener
docker build -t searchpy-webhook-listener:dev .
```

### Run Tests

```bash
# TODO: Add pytest tests
docker run --rm searchpy-webhook-listener:dev pytest
```

### Contribute

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📚 Documentation

- [Full Architecture](https://github.com/votre-user/searchpy-webhook-listener/wiki/Architecture)
- [Security Guide](https://github.com/votre-user/searchpy-webhook-listener/wiki/Security)
- [Troubleshooting](https://github.com/votre-user/searchpy-webhook-listener/wiki/Troubleshooting)

## 📝 Changelog

See [CHANGELOG.md](CHANGELOG.md) for detailed version history.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- [monim1/autoheal](https://github.com/monim1/autoheal) - Docker autoheal with webhook support
- [willfarrell/autoheal](https://github.com/willfarrell/autoheal) - Original autoheal project
- Flask framework
- Docker community

## 💬 Support

- 🐛 [Report a bug](https://github.com/votre-user/searchpy-webhook-listener/issues/new?template=bug_report.md)
- 💡 [Request a feature](https://github.com/votre-user/searchpy-webhook-listener/issues/new?template=feature_request.md)
- 💬 [Discussions](https://github.com/votre-user/searchpy-webhook-listener/discussions)

---

⭐ If this project helped you, consider giving it a star!

Made with ❤️ for the DevOps community
