# Ollama Cloud Models

Piotr runs Ollama in Docker Compose and the AI server talks to that local
Ollama endpoint. Cloud models do not require AI server code changes: sign in to
Ollama inside the `ollama` container, pull a cloud model, then use a model name
ending in `-cloud`.

The default AI server config stays local:

```yaml
agent:
  type: polite_reply
  model: qwen3:4b
```

## Setup and Smoke Test

Run the helper from the repository root:

```bash
tools/ollama-cloud-setup-test.sh
```

By default it:

- starts the `ollama` Compose service
- runs interactive `ollama signin` inside the container
- pulls `gpt-oss:120b-cloud`
- sends a short request to `http://127.0.0.1:11434/api/chat`

Use a different cloud model:

```bash
tools/ollama-cloud-setup-test.sh --model MODEL-NAME-CLOUD
```

Skip sign-in after the container is already authenticated:

```bash
tools/ollama-cloud-setup-test.sh --no-signin
```

## Manual Commands

Start the local Ollama service:

```bash
docker compose up -d ollama
```

Sign in inside the container:

```bash
docker compose exec ollama ollama signin
```

Pull a cloud model:

```bash
docker compose exec ollama ollama pull gpt-oss:120b-cloud
```

Test the local Ollama API:

```bash
curl http://127.0.0.1:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss:120b-cloud",
    "messages": [{"role": "user", "content": "Reply with exactly: piotr cloud ok"}],
    "stream": false
  }'
```

## Using a Cloud Model Temporarily

Keep `ai_server/config.compose.yaml` local by default. To run the AI server with
a cloud model, use a separate config file and set only the model fields you want
to offload, for example:

```yaml
agent:
  type: polite_reply
  model: gpt-oss:120b-cloud
```

Then run:

```bash
tools/ai-server.sh --config path/to/cloud-config.yaml
```

For orchestrator configs, set the top-level `agent.model` and any per-domain
agent `model` values that should use cloud inference.
