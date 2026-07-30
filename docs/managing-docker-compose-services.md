# Managing Docker Compose Services

## Document status

- **Authority:** Operational guide
- **Audience:** Operators and agents managing the repository's Compose services
- **Read when:** Inspecting, stopping, removing, or restarting Compose services

Run these commands from the repository root.

Show running services:

```bash
docker compose --env-file config/services.env ps
```

Stop background services:

```bash
docker compose --env-file config/services.env stop ollama wyoming-piper
```

Stop and remove the Compose stack:

```bash
docker compose --env-file config/services.env down
```

Enable Ollama cloud-model offload for the local Docker Ollama service:

```bash
tools/ollama-cloud-setup-test.sh --services-config config/services.env
```

See [Ollama Cloud Models](ollama-cloud-models.md).

## Main local-model GPU residency

When the AI-server configuration contains top-level `agent.local_model`, the
server treats that model as its protected local workhorse:

- one process-wide Ollama connection forces `keep_alive: -1` on its requests;
- startup verifies the model before microphone or websocket admission begins;
- verification repeats every 60 seconds;
- each residency inspection, unload, or load request has a 60-second bound;
- full GPU residency requires the Ollama `/api/ps` entry to report
  `size_vram == size`.

If the model is absent or partially CPU-resident, the server waits for active
Ollama requests to finish, temporarily blocks new requests, unloads competing
Ollama models, unloads and reloads `agent.local_model`, and checks it again.
DSA-specific `local_model` overrides are not protected and may be evicted.

Successful checks are logged at `DEBUG`. Startup verification and successful
repair are logged at `INFO`; detection and corrective actions are logged at
`WARNING`. If repair still cannot make the model fully GPU-resident, startup
fails or a running AI server exits non-zero. Compose then restarts the service
because it uses `restart: unless-stopped`; a manually launched server must be
started again after the GPU-memory cause is corrected.

Inspect the AI-server decision log first:

```bash
docker logs --tail 200 piotr-ai-server-1
```

Then inspect Ollama's current placement:

```bash
curl -s http://127.0.0.1:11434/api/ps
```
