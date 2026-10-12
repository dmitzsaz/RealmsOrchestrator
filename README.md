# RealmsOrchestrator (Under heavy development)

**RealmsOrchestrator** runs Minecraft worlds on pluggable providers (local Docker, Proxmox LXC, Northflank, AWS Fargate), featuring Cloudflare R2 storage integration, crash-safe backups, automated RCON control, and automatic whitelist/admin management.

## Features

- One provider interface (`providers/base.py`): `baremetal`, `proxmoxlxc`, `northflank`, `awsfargate`.
- A node agent inside every world container: downloads the world, supervises Minecraft, runs RCON, opens reverse tunnels. Nodes need no public IP.
- Cloudflare R2 integration for world storage (compatible with AWS S3 API). Agents only receive presigned URLs.
- Temp snapshots every N minutes; on a crash the newest snapshot becomes the world's backup, a normal stop saves to cold storage.
- The orchestrator re-attaches to running worlds after a restart.
- Automatic assignment of admins (`op`) and whitelist management via RCON.
- Auto shutdown after 5 minutes if no players are active.
- Simple REST API for world and player management.

See [docs/providers.md](docs/providers.md) for the architecture, provider settings and the mcproxy contract.

## Quick Start

### 1. Prerequisites
- Python 3.9+
- Docker (for the `baremetal` provider and to build the runtime image)
- MySQL 5.7.8+ (JSON columns)
- Cloudflare R2 account

Install dependencies:
```bash
pip install -r requirements.txt
```

The runtime image (`ghcr.io/<owner>/realms-runtime:dev`) is built by GitHub Actions on every push to `agent/`. To build it yourself:
```bash
docker buildx build --platform linux/amd64 -t ghcr.io/<you>/realms-runtime:dev --push agent/
```

### 2. Configure `.env` or `config.py`

Example configuration:
```
DB_USER_NAME=...
DB_ROOT_PASSWORD=...
DB_NAME=...
DB_HOST=localhost
DB_PORT=3306
R2_ACCESS_KEY=...
R2_SECRET_KEY=...
R2_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com
R2_BUCKET=...
LISTEN_PORT=8080

RUNTIME_IMAGE=ghcr.io/<you>/realms-runtime:dev
ENABLED_PROVIDERS=baremetal,northflank
DEFAULT_PROVIDER=baremetal
# Agents connect here; the only port remote providers must reach.
AGENT_LISTEN_PORT=8081
# TLS is built in and pinned by the agents, no certificate needed.
ORCHESTRATOR_PUBLIC_URL=http://<public-ip>:8081
AGENT_SECRET=<random>
TEMP_SAVE_INTERVAL_MINUTES=10

# mcproxy integration
MCPROXY_AUTH_URL=http://mcproxy:8080
INTERNAL_API_TOKEN=<random, shared with mcproxy>
PROXY_TUNNEL_ADDR=proxy.example.com:25570

# per provider, e.g.
BAREMETAL_NETWORK=realms
NORTHFLANK_API_TOKEN=...
NORTHFLANK_PROJECT_ID=...
```

### 3. Run MySQL (optional)
```bash
docker run --name mysql-realms \
  -e MYSQL_ROOT_PASSWORD=yourpassword \
  -e MYSQL_DATABASE=realms \
  -p 3306:3306 \
  -d mysql:8.0
```

### 4. Launch the server
```bash
python main.py
```

## API

| method | path | |
|---|---|---|
| GET | `/worlds/{id}/start[?provider=name]` | start (or describe) a world |
| GET | `/worlds/{id}/stop` | stop, final save to cold storage in background |
| POST | `/worlds/{id}/backup` | upload a temp snapshot now |
| GET | `/worlds/active` | placed worlds with provider, addresses and agent state |
| POST | `/internal/worlds/{id}/connect` | for mcproxy (`Authorization: Bearer INTERNAL_API_TOKEN`) |
