# Managing Docker Compose Services

Run these commands from the repository root.

Show running services:

```bash
docker compose ps
```

Stop background services:

```bash
docker compose stop ollama wyoming-piper wyoming-whisper
```

Stop and remove the Compose stack:

```bash
docker compose down
```

Enable Ollama cloud-model offload for the local Docker Ollama service:

```bash
tools/ollama-cloud-setup-test.sh
```

See [Ollama Cloud Models](ollama-cloud-models.md).
