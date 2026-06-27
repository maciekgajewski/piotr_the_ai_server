# Managing Docker Compose Services

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
