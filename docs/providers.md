# Providers, storage and connectivity

Worlds can run on several providers. Every provider runs the same runtime image,
`itzg/minecraft-server` with a small node agent (`agent/`). The provider only places
and removes that container. Everything else is the agent's job and works the same everywhere.

```
                 management API :8080 (internal)
   clients ───────────────────────────────┐
                                          ▼
                              ┌───────────────────────┐   presigned URLs   ┌────┐
   mcproxy ── /internal/... ─►│     orchestrator      │──────────────────► │ R2 │
      ▲                       │  WorldManager + Hub   │                    └────┘
      │                       └──────────▲────────────┘                      ▲
      │ RTUN1 reverse tunnel             │ WebSocket :8081 (outbound from node)│ GET/PUT
      │ (outbound from node)             │                                    │
      │                       ┌──────────┴────────────┐                       │
      └───────────────────────│ agent ── Minecraft    │───────────────────────┘
                              │ (baremetal | proxmoxlxc | northflank | awsfargate)
                              └───────────────────────┘
```

The node never needs a public address. It opens three kinds of outbound connections:
the control WebSocket to the orchestrator, HTTPS to R2, and reverse tunnels to mcproxy.

## Lifecycle

| Step | What happens |
|---|---|
| start | A session ID is stored, then `provider.launch()` runs. The agent connects and asks for `bootstrap`, which returns a presigned GET for the cold archive plus a presigned PUT for its temp key. The agent extracts the archive into `/data/<level>` and starts `/start`. |
| ready | Local RCON answers. The orchestrator sets up the whitelist and ops, fills `ips`/`ports`, and sets `status=running`. |
| running | The agent sends a heartbeat with the player list every 5 s. Admin op sync for newAuth uses the same logic as before. After `EMPTY_STOP_MINUTES` with nobody online, the world stops. |
| temp snapshot | Runs every `TEMP_SAVE_INTERVAL_MINUTES`, only if players were online since the last one. The agent does `save-off`, then `save-all flush`, zips the world, does `save-on`, and PUTs to `temp/mc_world_{id}_{session}.zip`. `POST /worlds/{id}/backup` takes one immediately. |
| stop | Status becomes `updating`. The agent stops Minecraft and uploads the final save to `mc_world_{id}.zip` (cold storage, as before). Then `provider.destroy()` runs, the temp object is deleted, the runtime columns are set to `NULL`, and status becomes `idle`. |
| crash | Any of: Minecraft exits on its own, the agent is silent past `AGENT_LOST_TIMEOUT`, or the provider reports the instance stopped or missing. The orchestrator destroys the instance, and the temp snapshot is copied over the cold archive **if it is newer**. Then status becomes `idle`. |
| orchestrator restart | `reconcile()` inspects every placed world. Live ones are re-attached, and an interrupted stop is resumed. Dead ones are recovered as crashes. |

Crash semantics in more detail:

* **Last-modified check.** A temp is promoted only if its LastModified is later than the cold archive's. A final save that finished just before a crash is therefore never rolled back.
* **Clean exit.** If Minecraft exits with code 0 by itself (for example `/stop`), the files are consistent. The agent uploads them as temp first, so that state gets promoted.
* **SIGTERM.** If the provider sends SIGTERM without an orchestrated stop (spot interruption, host shutdown), the agent stops Minecraft and uploads a temp snapshot before the provider's kill timeout.

Temp keys are scoped to the session, so an agent from an old run can never overwrite anything that matters. Add an R2 lifecycle rule that deletes `temp/` objects after about 7 days to clean up orphans.

## Database

New `worlds` columns, added automatically at startup (`database/db.py`):

| column | while running | stopped |
|---|---|---|
| `provider` | `baremetal`, `proxmoxlxc`, `northflank`, `awsfargate` | NULL |
| `ips` | `{"private": "10.0.0.5", "public": "1.2.3.4" \| null}` | NULL |
| `ports` | `{"private": 25565, "public": 31234 \| null}` (port reachable on each IP) | NULL |
| `instanceId` | container ID / `node/vmid` / Northflank service ID / ECS task ARN | NULL |
| `sessionId` | per-launch nonce; binds the agent token and the temp key | NULL |

`instanceId` and `sessionId` are what let the orchestrator manage a world again after it restarts.

## Providers

`providers/base.py` defines `Provider`, which has three methods: `launch(spec)`, `inspect(id)` and `destroy(id)`.
A provider is chosen per start in this order:

1. `GET /worlds/{id}/start?provider=…`
2. `params.provider` on the world
3. `DEFAULT_PROVIDER`

Only providers listed in `ENABLED_PROVIDERS` are loaded.

Every provider also accepts `<PREFIX>_ORCHESTRATOR_URL`, `_CONNECT_MODE`, `_RUNTIME_IMAGE`, `_MCPROXY_AUTH_URL`, `_CPU` and `_MEMORY_MB`.

| provider | prefix | default connect mode | notes |
|---|---|---|---|
| baremetal | `BAREMETAL_` | private | Docker via the socket or `BAREMETAL_DOCKER_HOST`. The container name is still `minecraft_{id}_{prefix}`, so the current mcproxy keeps working. Set `BAREMETAL_NETWORK` to mcproxy's network. |
| proxmoxlxc | `PROXMOX_` | private | Requires PVE ≥ 9.1, where OCI application containers are a technology preview. The image is pulled with `oci-registry-pull`. The container is created stopped, our `env` is merged into its config (PVE would otherwise replace it with the image's ENV), and then it is started. The API cannot exec into an LXC, which is why the agent dials out. |
| northflank | `NORTHFLANK_` | tunnel | Only HTTP ports can be public on Northflank (TCP needs a paid L4 load balancer), so tunnel mode is the default. The API allows 1000 requests per hour, so `inspect` is called only during boot or when heartbeats stop. `NORTHFLANK_EXPIRY_SECONDS` is a safety net against orphaned services. |
| awsfargate | `AWS_FARGATE_` | tunnel | Uses an existing task definition, with env, CPU and memory passed as `run_task` overrides. Set `stopTimeout: 120` on the container. Without `ASSIGN_PUBLIC_IP` the subnet needs a NAT gateway for the agent's outbound traffic. `public` mode dials the task's public IP, which needs 25565 open in the security group. |

## Connect modes and the mcproxy contract

`private` and `public` mean mcproxy dials `ips[mode]:ports[mode]` directly. `tunnel` means the agent dials mcproxy.

mcproxy calls:

```
POST /internal/worlds/{id}/connect
Authorization: Bearer $INTERNAL_API_TOKEN
{"ticket": "<random [A-Za-z0-9_-]{16,128}>"}      # required for tunnel worlds

200 {"mode": "direct", "address": "10.0.0.5:25565"}
200 {"mode": "tunnel"}            # the agent has already dialed PROXY_TUNNEL_ADDR
503 world not running / agent offline
```

The tunnel flow is:

1. mcproxy creates the ticket and registers it, e.g. with a 10 s TTL and single use.
2. mcproxy calls `/connect`.
3. The orchestrator sends `tunnel` to the agent over the control channel.
4. The agent connects to `127.0.0.1:25565` inside the container, then opens TCP (TLS if `PROXY_TUNNEL_TLS`) to `PROXY_TUNNEL_ADDR`.
5. The agent writes `RTUN1 <ticket>\n` and from then on relays raw bytes.

mcproxy should match the ticket and use that connection exactly as it would use a dialed backend: write the handshake and Login Start, then relay.

The ticket is created by mcproxy before it calls `/connect`, so the agent's connection can never arrive before the ticket is registered. The orchestrator only ever sends the agent to the configured `PROXY_TUNNEL_ADDR`, never to an address chosen by the caller.

### mcproxy setup (RealmsOrchestratorProxy ≥ c0707a8)

| mcproxy env | |
|---|---|
| `ORCHESTRATOR_URL` | management API, e.g. `http://orchestrator:8080` (already used for starting worlds) |
| `INTERNAL_API_TOKEN` | same value as the orchestrator's |
| `TUNNEL_LISTEN_ADDR` | e.g. `:25570`; must be reachable from the nodes. The orchestrator's `PROXY_TUNNEL_ADDR` is its public `host:port` |
| `TUNNEL_TLS_CERT`, `TUNNEL_TLS_KEY` | optional TLS for tunnels; set `PROXY_TUNNEL_TLS=true` on the orchestrator |
| `DOCKER_PROVIDERS` | providers resolved through the local Docker API, default `baremetal` |

How mcproxy uses these settings:

* **Resolution.** mcproxy reads `id, provider, status` by `domainPrefix`.
  * Worlds without a provider, or on a Docker provider, are resolved through Docker exactly as before.
  * Any other world is reached through `/connect`. While the world is not `running`, it counts as starting: Limbo waits and keeps retrying.
* **newAuth sessions.** Sessions to orchestrator-placed backends are also bound to the world ID. A relayed `hasJoined` with a valid `X-Realms-Internal-Token` and a matching `X-Realms-World-Id` is accepted. For tunnel backends, the expected `ip=` is `127.0.0.1`.

## newAuth on remote nodes

Backends of newAuth worlds use mcproxy's session API (`-Dminecraft.api.*`).

* **baremetal** uses `MCPROXY_AUTH_URL` directly, as before.
* **Other providers** point the JVM at the agent's local relay (`127.0.0.1:25580`). The relay forwards a fixed allow-list of auth paths over the control channel to the orchestrator, which calls `MCPROXY_AUTH_URL` with the world headers described above. URLs in `/discovery` responses are rewritten from `MCPROXY_AUTH_PUBLIC_URL` to the relay. This keeps mcproxy's auth API internal.
* To skip the relay, set `<PREFIX>_MCPROXY_AUTH_URL` to an address the node can reach.

## Security notes

* Only `AGENT_LISTEN_PORT` (8081) has to be reachable from remote nodes. Put it behind TLS (`ORCHESTRATOR_PUBLIC_URL=https://…`). The management API on 8080 is still unauthenticated and must stay internal.
* Agent tokens are `HMAC(AGENT_SECRET, world.session)`. A connection is rejected (HTTP 410) as soon as the world's `sessionId` changes.
* Agents get presigned URLs for their own keys only, never R2 credentials.
* The RCON password is random per session and never leaves the container. RCON is no longer published on the host.
* `/internal/*` is disabled unless `INTERNAL_API_TOKEN` is set.

## Upgrading from master

1. Stop all worlds with the old version. Containers it started have no `provider` and are ignored by `reconcile()`.
2. Build and push the runtime image (`docker build -t … agent/`), then set `RUNTIME_IMAGE`.
3. Expose `AGENT_LISTEN_PORT` to the providers you enable and set `ORCHESTRATOR_PUBLIC_URL` (or `<PREFIX>_ORCHESTRATOR_URL`). For baremetal the default `http://host.docker.internal:8081` works when the orchestrator runs on the Docker host.

The schema migration runs automatically.

Limits worth knowing:

* A single R2 PUT is capped at about 5 GB per archive.
* Fargate gives at most 120 s between SIGTERM and SIGKILL.
* `GET /worlds/{id}/start` blocks while the provider creates the instance. For Proxmox that includes the image pull the first time.
