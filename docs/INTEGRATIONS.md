# Integrations: front-ends and tools

**English** · [Español](es/INTEGRACIONES.md)

DLLM exposes the OpenAI API at `http://<orchestrator>:8080/v1`. Test and configure it from the panel's **Connect** tab,
which fills in these values with your URL and key.

## Common settings
- **Base URL:** `http://IP:8080/v1` · **API key:** the panel's (required if the orchestrator listens outside loopback).
- **Model:** the name shown in `/v1/models`; without `model` the main model answers.
- **Context:** the one you used when loading the model (`--ctx`).

## AnythingLLM
Settings → LLM provider → *Generic OpenAI* with the URL, the key and the model name. Embeddings (RAG): load a model in
"embeddings mode" and choose it as *Generic OpenAI* in Embedder. `@agent skills` need a model with the 🛠 (tools) badge.

## Tools / function calling
1. DLLM detects in the GGUF whether the chat template supports tools and starts `llama-server --jinja`.
   Per-model setting `tools`: `auto` (default), `on` (force) or `off`.
2. The front-end sends `tools` and receives `tool_calls`; **it runs** the tool and returns the result.
3. Real check: Connect → *Test* (`POST /api/tools/test`) asks for a sample function call.
A model without a tool template still answers chat, but the front-end must use its prompt-based agent mode.

## Clients that only speak Ollama
Use `http://IP:11434` as the Ollama URL (see [USAGE](USAGE.md#ollama-compatible-api)). Open WebUI can use either the
OpenAI connection (`/v1`) or the Ollama one; the Connect tab has the snippet.

## Open WebUI, curl, Python
See the snippets in the Connect tab (`openai` SDK: `base_url`, `api_key`, `model`).

## What is tested
**Simulated** server and binaries (tests): passing `tools`/`tool_calls`, `--jinja`, capabilities. **Not tested** yet with
real models or with real AnythingLLM/Open WebUI.
