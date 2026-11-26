# Roadmap and pending work

**English** · [Español](es/ROADMAP.md)

Status: 0.8.x series (the current version is in `dllm/__init__.py`; no releases published on GitHub). This is intent,
not commitment.

## 0.5.0 (done; included in 0.6.0)
- **Phase 1** (done): link probe over the authenticated channel, network tiers and splitting with a hop penalty.
- **Phase 2** (done, tested with simulations): isolated model slots (one `llama-server` per slot on the orchestrator,
  one `rpc-server` and one HMAC tunnel per slot on each node) and a memory ledger per node (VRAM Ledger).
- **Phase 3** (done, tested with simulations; includes `device_policy`, VRAM+RAM ledger, queue and 429/503): L7 proxy
  `/v1/*` that routes by the `model` field (JSON and SSE), `pinned` / `on-demand` policies with LRU.

### Abstractions included in 0.5.0 (done: 1, 2 and 3; 4 is pending) so the core does not need refactoring later
1. **`EngineDriver`**: a slot does not assume `llama-server`. Interface with `build_command()`, `healthcheck()`,
   `estimate_vram()` and `supported_endpoints()`; `LlamaCppDriver` is the first implementation (with `--mmproj` and
   `--embeddings`).
2. **L7 router by route table and modality**: each OpenAI route (`/v1/chat/completions`, `/v1/embeddings` and, in the
   future, `/v1/images/generations`, `/v1/videos/generations`, `/v1/audio/transcriptions`, `/v1/audio/speech`) is
   assigned to the slot whose driver supports it; supports JSON, SSE and binary/multipart responses.
3. **Dual planning in the ledger**: *Model-Split* (split over RPC across nodes, for large LLMs) and *Task-Routing / Task
   Affinity* (pinned to a single optimal node and GPU, for embeddings, audio or images), so different GPUs serve
   different modalities in parallel.
4. **Engine-parameterized binary manager** (pending): downloading and syncing `bNNNN` builds is generalized to manage
   other C/C++ binaries alongside llama.cpp.

## Vision v1.0: distributed multimodal orchestrator
Also manage image, short video and audio (STT/TTS) generation on the cluster's heterogeneous GPUs, with the same rules:
standard library only in `dllm/`, native C/C++ binaries (`ggml`), everything on `127.0.0.1` and HMAC tunnels. Each
engine comes in as a new driver:
- **`StableDiffusionCppDriver`** (`stable-diffusion.cpp`, `sd-server`): GGUF models (FLUX.1, SDXL, Wan 2.1,
  HunyuanVideo); `/v1/images/generations` and `/v1/videos/generations`; `on-demand` loading and LRU unloading.
- **`WhisperCppDriver`** (`whisper.cpp`, `whisper-server`): `/v1/audio/transcriptions`.
- **`TtsDriver`** (`kokoro.cpp` / `piper`): `/v1/audio/speech`.
- **Heterogeneous planning by hardware affinity**: assign each modality to the most suitable node (main LLM on the GPU
  with most VRAM; STT/TTS/image on secondary nodes) and batch parallelism (*batch queue*) across nodes.

## Other pending work
- Routing does not retry if a slot dies mid-request.
- Calibrate `TIER1_MBPS` with real measurements (the probe uses a single TCP stream in Python).
- Run the test suite on Windows and macOS (only Linux is tested in each version).
- Test in real environments: Windows (shortcut, hidden console, firewall with UAC), CUDA on Linux, macOS, broadcast
  discovery between computers.
- Decide whether the panel may open without a key from the LAN too (today it contradicts the security rule).
- Publish the first release.
