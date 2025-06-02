# RealmsOrchestrator (Under heavy development)

**RealmsOrchestrator** is a Docker-based orchestrator for managing Minecraft worlds, featuring Cloudflare R2 storage integration, automated RCON control, and automatic whitelist/admin management.

## Features

- Easy management of Minecraft worlds in Docker containers.
- Cloudflare R2 integration for world storage (compatible with AWS S3 API).
- Automatic assignment of admins (`op`) and whitelist management via RCON.
- Auto shutdown after 5 minutes if no players are active.
- Asynchronous world uploads and downloads with non-blocking API.
- Simple REST API for world and player management.

## Quick Start

### 1. Prerequisites
- Python 3.8+
- Docker (docker-compose optional)
- MySQL or SQLite (MySQL default)
- Cloudflare R2 account

Install dependencies:
```bash
pip install -r requirements.txt
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
LISTEN_ADDR=0.0.0.0:8080
```

### 3. Run MySQL (optional)
```bash
docker run --name mysql-realms \
  -e MYSQL_ROOT_PASSWORD=yourpassword \
  -e MYSQL_DATABASE=realmsdb \
  -p 3306:3306 \
  -d mysql:8.0
```

### 4. Launch the server
```bash
python main.py
```
