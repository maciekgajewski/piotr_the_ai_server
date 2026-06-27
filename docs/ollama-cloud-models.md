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
tools/ollama-cloud-setup-test.sh --services-config config/services.env
```

By default it:

- starts the `ollama` Compose service
- runs interactive `ollama signin` inside the container
- pulls `gpt-oss:20b-cloud`
- sends a short request to `http://127.0.0.1:11434/api/chat`

Use a different cloud model:

```bash
tools/ollama-cloud-setup-test.sh --services-config config/services.env --model MODEL-NAME-CLOUD
```

Skip sign-in after the container is already authenticated:

```bash
tools/ollama-cloud-setup-test.sh --services-config config/services.env --no-signin
```

## Manual Commands

Start the local Ollama service:

```bash
docker compose --env-file config/services.env up -d ollama
```

Sign in inside the container:

```bash
docker compose --env-file config/services.env exec ollama ollama signin
```

Pull a cloud model:

```bash
docker compose --env-file config/services.env exec ollama ollama pull gpt-oss:20b-cloud
```

Test the local Ollama API:

```bash
curl http://127.0.0.1:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss:20b-cloud",
    "messages": [{"role": "user", "content": "Reply with exactly: piotr cloud ok"}],
    "stream": false
  }'
```

## Using a Cloud Model Temporarily

Keep `ai_server/test-config.yaml` local by default. To run the AI server with
a cloud model, use a separate config file and set only the model fields you want
to offload, for example:

```yaml
agent:
  type: polite_reply
  model: gpt-oss:20b-cloud
  fallback_model: qwen3:4b-instruct
```

Then run:

```bash
tools/ai-server.sh --services-config config/services.env --config path/to/cloud-config.yaml
```

For orchestrator configs, keep `agent.orchestrator_model` on a small routing
model, set `agent.cloud_model` to the cloud DSA model, set `agent.local_model`
to the local fallback model, and use any per-domain agent `model` values only
when a specific DSA should override the global cloud model.
