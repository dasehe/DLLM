# Hoja de ruta y pendientes

Estado: 0.6.0 (primera versión de DLLM, empaquetada en zip; aún sin release publicada en GitHub). Lo de aquí es intención, no compromiso.

## 0.5.0 (hecho; incluido en 0.6.0)
- **Fase 1** (hecha): sonda de enlace por el canal autenticado, tiers de red y reparto con penalización por saltos.
- **Fase 2** (hecha, probada con simulaciones): slots de modelo aislados (un `llama-server` por slot en el orquestador, un `rpc-server` y un túnel HMAC
  por slot en cada nodo) y libro de memoria por nodo (VRAM Ledger).
- **Fase 3** (hecha, probada con simulaciones; incluye `device_policy`, ledger VRAM+RAM, cola y 429/503): proxy L7 `/v1/*` que enruta por el campo `model` (JSON y SSE), políticas `pinned` / `on-demand` con LRU.

### Abstracciones incluidas en 0.5.0 (hechas: 1, 2 y 3; la 4 queda pendiente) para no refactorizar el núcleo después (sin implementar más motores)
1. **`EngineDriver`**: un slot no asume `llama-server`. Interfaz con `build_command()`, `healthcheck()`,
   `estimate_vram()` y `supported_endpoints()`; `LlamaCppDriver` es la primera implementación (con `--mmproj` y
   `--embeddings`).
2. **Enrutador L7 por tabla de rutas y modalidad**: cada ruta OpenAI (`/v1/chat/completions`, `/v1/embeddings` y, a
   futuro, `/v1/images/generations`, `/v1/videos/generations`, `/v1/audio/transcriptions`, `/v1/audio/speech`) se
   asigna al slot cuyo driver la soporte; admite respuestas JSON, SSE y binarias/multipart.
3. **Planificación dual en el ledger**: *Model-Split* (repartido por RPC entre nodos, para LLM grandes) y
   *Task-Routing / Task Affinity* (fijado a un único nodo y GPU óptimos, para embeddings, audio o imagen), de modo que
   GPUs distintas atiendan modalidades distintas en paralelo.
4. **Gestor de binarios parametrizable por motor** (pendiente, v0.6.0): la descarga y sincronización de builds `bNNNN` se generaliza para
   poder gestionar otros binarios C/C++ junto a llama.cpp.

## Visión v0.6.0 / v1.0: orquestador multimodal distribuido
Gestionar también generación de imagen, vídeo corto y audio (STT/TTS) sobre las GPU heterogéneas del clúster, con las
mismas reglas: solo biblioteca estándar en `dllm/`, binarios nativos C/C++ (`ggml`), todo en `127.0.0.1` y túneles
HMAC. Cada motor entra como un driver nuevo:
- **`StableDiffusionCppDriver`** (`stable-diffusion.cpp`, `sd-server`): modelos GGUF (FLUX.1, SDXL, Wan 2.1,
  HunyuanVideo); `/v1/images/generations` y `/v1/videos/generations`; carga `on-demand` y descarga por LRU.
- **`WhisperCppDriver`** (`whisper.cpp`, `whisper-server`): `/v1/audio/transcriptions`.
- **`TtsDriver`** (`kokoro.cpp` / `piper`): `/v1/audio/speech`.
- **Planificación heterogénea por afinidad de hardware**: asignar cada modalidad al nodo más adecuado (LLM principal en
  la GPU con más VRAM; STT/TTS/imagen en nodos secundarios) y paralelismo por lotes (*batch queue*) entre nodos.

- Driver de imagen (`StableDiffusionCppDriver`) con reparto entre GPU (p. ej. codificador de texto en una GPU y DiT en
  otra) y cola por lotes; el driver de voz (`WhisperCppDriver`) con política `cpu_only` o GPU dedicada.

## Otros pendientes
- Panel: mostrar slots, ledger y selector de slot/fijado en *Modelos* (hoy solo API y CLI).
- Métricas de tok/s y recuperación ante caída por slot en el panel; el enrutado no reintenta si un slot muere a mitad.
- Añadir el fichero `LICENSE` (texto completo de GNU AGPL v3.0).
- Calibrar `TIER1_MBPS` con mediciones reales (la sonda usa un solo flujo TCP en Python).
- Investigar el test intermitente `test_1_full_cluster_from_installer`.
- Limpiar avisos F de ruff en tests (imports sin usar).
- Probar en entornos reales: Windows (acceso directo, consola oculta, cortafuegos con UAC), CUDA en Linux, macOS,
  anuncio por broadcast entre equipos.
- Decidir si el panel puede abrirse sin clave también desde la LAN (hoy contradice la regla de seguridad).
- Recuperar la CI de GitHub Actions (facturación) y publicar la release 0.6.0.
