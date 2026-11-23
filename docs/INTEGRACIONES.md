# Integraciones: front-ends y herramientas

DLLM expone la API de OpenAI en `http://<orquestador>:8080/v1`. Se prueba y configura desde la pestaña **Conectar** del
panel, que rellena estos datos con tu URL y clave.

## Ajustes comunes
- **Base URL:** `http://IP:8080/v1` · **API key:** la del panel (obligatoria si el orquestador escucha fuera de loopback).
- **Modelo:** el nombre que aparece en `/v1/models`; sin `model` responde el modelo principal.
- **Contexto:** el que usaste al cargar el modelo (`--ctx`).

## AnythingLLM
Ajustes → Proveedor de LLM → *Generic OpenAI* con la URL, la clave y el nombre del modelo. Embeddings (RAG): carga un
modelo en «modo embeddings» y elígelo como *Generic OpenAI* en Embedder. Los `@agent skills` requieren un modelo con la
insignia 🛠 (herramientas).

## Herramientas / function calling
1. DLLM detecta en el GGUF si la plantilla de chat admite herramientas y arranca `llama-server --jinja`.
   Ajuste por modelo `tools`: `auto` (por defecto), `on` (forzar) u `off`.
2. El front-end envía `tools` y recibe `tool_calls`; **él ejecuta** la herramienta y devuelve el resultado.
3. Comprobación real: Conectar → *Probar* (`POST /api/tools/test`) pide una llamada de función de ejemplo.
Un modelo sin plantilla de herramientas seguirá respondiendo a chat, pero el front-end deberá usar su modo de agente por prompt.

## Open WebUI, curl, Python
Ver los fragmentos de la pestaña Conectar (`openai` SDK: `base_url`, `api_key`, `model`).

## Qué está probado
Servidor y binarios **simulados** (tests): paso de `tools`/`tool_calls`, `--jinja`, capacidades. **No probado** aún con
modelos reales ni con AnythingLLM/Open WebUI reales.
