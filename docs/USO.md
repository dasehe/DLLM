# Uso

## El comando `dllm`

Tras instalar, el programa está en `~/.dllm/dllm.pyz`. Para escribir solo `dllm`, crea un alias:

```bash
# Linux / macOS (añádelo a ~/.bashrc o ~/.zshrc)
alias dllm='python3 ~/.dllm/dllm.pyz'
```
```powershell
# Windows PowerShell (añádelo a $PROFILE)
function dllm { py -3 "$env:USERPROFILE\.dllm\dllm.pyz" @args }
```

Desde el código fuente: `python -m dllm …`.

## Panel web

Ábrelo con el acceso directo **DLLM** (o `dllm open`): arranca DLLM si está parado, abre
**http://127.0.0.1:8080** y esconde la consola, que reaparece si cierras el navegador para reabrirlo o parar DLLM.
En el propio orquestador no pide clave. Desde otro equipo (si instalaste con *exponer API*) usa el **enlace para otros
equipos** que da el asistente o el botón **API key** del panel en el orquestador: guarda la clave en ese navegador. La cabecera muestra el estado (`IDLE` sin modelo, `LOADING`,
`READY`, `RECOVERING` si un nodo cayó y se está recargando, `ERROR`), el modelo cargado y los tokens/s en directo.

| Pestaña | Para qué |
|---|---|
| **Monitor** | Estado del clúster y de cada nodo en tiempo real (ver abajo) |
| **Nodos** | Tarjetas de nodo (modelos cargados, red, aporte), **Añadir por IP**, **Buscar en la red**, **Medir la red**, **Eliminar**, y **Configurar** cada uno (ver [Configurar cada nodo](#configurar-cada-nodo)); **Equipos esperando unirse** para reclamar los instalados sin código ([INSTALACION](INSTALACION.md#unir-un-nodo-sin-código-reclamarlo-desde-el-orquestador)) |
| **Modelos** | Recursos del clúster, cargar o **simular** un modelo eligiendo nodos y GPU/CPU, modelos en memoria, parámetros de cada modelo, añadir desde Hugging Face u Ollama (ver [Modelos](#pestaña-modelos)) |
| **Chat** | Conversación de prueba (con historial) contra el modelo cargado; adjunta imagen/audio si es multimodal |
| **Conectar** | URL, clave y fragmentos para front-ends; prueba de herramientas (ver [Conectar](#conectar-front-ends-y-herramientas)) |
| **Registros** | Qué ha pasado, dónde y por qué: eventos con nivel, modelo y nodo, filtros y búsqueda, último fallo con la causa (ver [Registros](#registros)) |
| **Sistema** | Copia de seguridad y restauración (ver [Copia de seguridad](#copia-de-seguridad-y-recuperación)) |

### Pestaña Modelos

De arriba abajo:

1. **Recursos del clúster**: VRAM, RAM y núcleos de CPU totales, lo **reservado** por los modelos cargados y lo libre,
   en total y por nodo (el orquestador figura aparte si no es también un nodo).
2. **Cargar o simular un modelo**. *Destino*: **sustituir el modelo principal** o **añadir otro modelo a la vez**.
   *Dónde ejecutar*: **Automático** (el planificador elige) o **Elegir nodos y dispositivo**: marca los nodos y, en cada
   uno, **GPU** o **CPU (RAM)**. Así un modelo pequeño puede usar la RAM de un nodo concreto y dejar la GPU libre para
   los grandes. Un nodo con GPU puede usar su CPU; «Orquestador (RAM local)» va solo (no se combina con nodos).
   **Simular** no carga nada: calcula el reparto y dibuja en *Recursos* en naranja lo que añadiría el modelo.
3. **Modelos en memoria**: una fila por modelo (qué es el «slot»: una ranura de memoria; el 0 es el principal).
   Muestra estado, **dónde corre** (nodo, GPU/CPU, GiB), uso y rendimiento (peticiones, errores, media, p95, tok/s).
   Se puede **Cancelar** una carga en curso, **Descargar** el principal o **Eliminar** cualquier otro, también si está
   en error. Si falla, enlaza con su registro.

### Registros

Cada evento lleva hora, **nivel** (info, aviso, error), modelo, nodo y texto; se filtra por nivel, modelo, nodo y
texto. Registra cargas y su duración, altas y caídas de nodos, recuperaciones, peticiones con error 5xx o rechazadas
(429/503) y descargas. Arriba, **Último fallo**: código y líneas de `llama-server` y, si murió un `rpc-server`, el del
nodo con su código y líneas. Se guardan en `events.jsonl` (carpeta de configuración, rota a 2 MiB × 3) y sobreviven a
reinicios.

### Monitor

![Monitor (datos simulados)](img/monitor.png)

Arriba: estado, **tokens/s** de generación con su gráfica, peticiones en curso (y uso del contexto si llama.cpp lo
publica) y nodos activos con la VRAM total en uso. Debajo, una tarjeta por nodo con su GPU, actualizada cada 2–3 s:

| Dato | NVIDIA (Windows/Linux) | AMD en Linux | AMD/Intel en Windows | Apple | Sin GPU |
|---|---|---|---|---|---|
| Uso de GPU | ✓ | ✓ | ✓ | ✓ | — |
| VRAM usada / total | ✓ | ✓ | ✓ | memoria en uso | — |
| Consumo y límite (W) | ✓ | ✓ | — | — | — |
| Temperatura | ✓ | ✓ | — | — | — |
| Ventilador | ✓ (%) | ✓ (rpm) | — | — | — |
| Reloj | ✓ | — | — | — | — |
| CPU, RAM, tráfico RPC | ✓ | ✓ | ✓ | ✓ (CPU aprox.) | ✓ |

Fuentes: `nvidia-smi`, el `sysfs` del driver amdgpu, los contadores del Administrador de tareas de Windows (WDDM) e
`ioreg`. Windows no ofrece temperatura ni consumo de GPUs AMD/Intel sin el software propietario del fabricante, por
eso aparecen como «—». El **tráfico RPC** mide los datos que pasan por el nodo: si sube mientras generas, ese nodo está
trabajando. Las gráficas guardan los últimos ~5 minutos.

### Añadir, buscar, medir y eliminar nodos

Interruptor **Modo avanzado** (cabecera): sin él se ve lo esencial; con él, todos los parámetros técnicos. Se recuerda en el navegador.

| Acción | Qué hace |
|---|---|
| **Añadir por IP** | IP o nombre (y puerto del agente, 8765 por defecto, en modo avanzado). Si el nodo ya usa el secreto del clúster se registra al instante; si está sin emparejar se inicia el emparejamiento (código de verificación, aceptar en el nodo); si está emparejado con otro secreto lo dice. Útil cuando el broadcast no llega (VLAN, VPN, Wi-Fi aislado) |
| **Buscar en la red** | Explora un rango **privado** (por defecto el /24 del orquestador; en modo avanzado se elige: `192.168.1.0/24` o `192.168.1.10-50`, máx. 1024 direcciones) buscando agentes DLLM. Muestra *libre* (Reclamar), *emparejado* (Añadir, por si es de tu clúster) o *ya en este clúster* |
| **Medir la red** | Mide **cada nodo por separado** (con todo el ancho) y luego **todos a la vez**, como en un trabajo conjunto: si el enlace del orquestador se reparte, el valor «en conjunto» cae y se avisa. Repítelo tras cambiar un cable o un conmutador. Un nodo concreto también se mide desde su tarjeta |
| **Usar igualmente** | Un nodo por debajo del mínimo (800 Mbps, `--min-mbps`) no entra en los repartos. Este modo lo permite **tras confirmar**; el plan avisa de que es modo forzado. «Dejar de forzar» lo revierte. Para pruebas |
| **Eliminar** | Quita el nodo de la lista (no lo desinstala). No se puede si un modelo lo usa. Si sigue vivo y anunciándose, se ignora 10 min. Los nodos fijados con `--node` reaparecen al reiniciar el orquestador |

### Configurar cada nodo

![Configurar un nodo (modo avanzado, datos simulados)](img/nodos.png)

**Nodos → Configurar**:

| Ajuste | Dónde vive | Efecto |
|---|---|---|
| Usar este nodo en los repartos | Orquestador | Desactivado = nunca se le asigna nada (sigue visible en el monitor) |
| Capas fijas | Orquestador | Cuántas capas del modelo lleva este nodo; el resto se reparte solo. Útil para afinar a mano |
| Ancho de banda de memoria | Orquestador | Ordena los nodos por velocidad (los más rápidos se llenan primero). Ej.: RTX 3060 = 360, RX 7600 XT = 288 GB/s |
| Dispositivo | Nodo | Qué GPU presta (o CPU). Automático = la de más memoria |
| Porcentaje / memoria / hilos | Nodo | Cuánto aporta (ver [Gestionar lo que aporta cada nodo](#gestionar-lo-que-aporta-cada-nodo)) |

Los ajustes «del nodo» solo se pueden cambiar desde el panel si el nodo está en modo automático. Todo se aplica en el
siguiente reparto, o al recargar el modelo si está en uso.

## Modelos: añadir, gestionar y ajustar

Todo desde el panel del orquestador. Los modelos se guardan en su carpeta de modelos (`hf/…`, `ollama/…` o donde
los copies tú).

### Añadir

| Origen | Cómo |
|---|---|
| **Hugging Face** | *Añadir modelos → Hugging Face*: busca (p. ej. `qwen2.5 7b instruct`), pulsa *Ver ficheros* y descarga la cuantización que quieras. Recomendado: **Q4_K_M** o **Q5_K_M**. Los modelos troceados (`-00001-of-0000N`) descargan todas sus partes. |
| **Ollama** (registro) | *Ollama*: escribe el nombre como en [ollama.com/library](https://ollama.com/library), p. ej. `qwen2.5:7b`. Se importan también sus parámetros por defecto (temperatura, paradas, prompt de sistema). |
| **Ollama local** | *Ollama local*: lista los modelos que ya tengas en Ollama **en el equipo del orquestador** y los importa. Si están en el mismo disco se enlazan sin ocupar espacio extra. |
| A mano | Copia el `.gguf` (y su `mmproj-*.gguf` si es multimodal) a la carpeta de modelos. |

Cada descarga muestra progreso, velocidad y tiempo restante, y se puede **cancelar**. Si vuelves a pedirla, **se
reanuda** desde donde quedó. Antes de empezar se comprueba que hay espacio libre; al terminar se verifica el sha256
publicado y que el fichero sea GGUF. Hasta entonces vive como `.part` y no aparece en la lista.

**Modelos con acceso restringido** de Hugging Face (Llama, Gemma…): acepta sus condiciones en la página del modelo y
guarda tu token (`hf_…`, de huggingface.co/settings/tokens) en *Token de Hugging Face*. Se guarda en el orquestador con
permisos 600 y solo se envía a huggingface.co.

Limitación con Ollama: funciona con la mayoría de modelos de texto, pero algunos modelos nuevos o multimodales
empaquetados para el motor propio de Ollama no los carga llama.cpp. En ese caso, usa la versión GGUF de Hugging Face.

### Gestionar

*Modelos instalados* muestra tamaño, arquitectura, capas, contexto máximo, cuantización, origen y si es
**👁 multimodal**. **Eliminar** borra el fichero (todas sus partes) y sus parámetros; no deja borrar el modelo cargado.

### Parámetros por modelo

![Parámetros de un modelo (modo avanzado, datos simulados)](img/modelo-parametros.png)

Botón **Parámetros** (con ● si tiene alguno guardado). Vacío = valor por defecto de llama.cpp.

**Carga** (se aplican al cargar el modelo; cambiarlos exige recargarlo):

| Parámetro | Qué hace | Orientación |
|---|---|---|
| Contexto | Memoria de trabajo de la conversación (tokens) | Ver «contexto máximo» en el reparto |
| Caché K / Caché V | Tipo de la caché de contexto | `q8_0` reduce la memoria del contexto casi a la mitad, sin pérdida apreciable; `q4_0` la reduce más, con algo de pérdida. La caché V cuantizada requiere flash attention |
| Flash attention | Atención optimizada (`auto`, `on`, `off`) | `auto` |
| Lote lógico / físico | Tokens del prompt que se procesan por paso (`-b` / `-ub`) | 2048 / 512; más alto = prompts largos más rápidos y más memoria |
| Peticiones simultáneas | Conversaciones en paralelo (`-np`) | El contexto se reparte entre ellas |
| RoPE (escalado, base, factor) | Extender el contexto más allá del de entrenamiento | Solo si sabes lo que haces; el modelo ya trae los suyos |
| Proyector multimodal | Visión/audio: automático, ninguno o uno concreto | Automático |

El reparto tiene en cuenta el tipo de caché: con `q8_0` cabe aproximadamente el doble de contexto.

**Generación** (en cada petición, sin pisar los valores que envíe el cliente):

| Parámetro | Qué hace | Orientación |
|---|---|---|
| Temperatura | Creatividad / aleatoriedad | 0,2–0,4 preciso · 0,7–0,8 equilibrado · >1 creativo |
| Top-k, Top-p, Min-p, Typical-p | Recortan las opciones poco probables | Déjalos vacíos salvo que el modelo recomiende otros |
| Penalización de repetición | Evita que repita frases | 1,0–1,15 |
| Penalización de presencia / frecuencia | Favorecen temas nuevos / castigan palabras repetidas | 0–0,5 |
| Máx. tokens por respuesta | Longitud máxima | Vacío = sin límite |
| Semilla | Fija = respuestas reproducibles | Vacío = aleatoria |
| Secuencias de parada | Texto que corta la respuesta | Separadas por comas |
| Prompt de sistema | Instrucciones fijas para cada conversación | p. ej. «Responde en español» |
| Fecha y hora para el modelo | Le indica la fecha y hora del **reloj del servidor** (`fecha y hora`, `solo la fecha` o `no indicarla`) | Por defecto, fecha y hora: los modelos no saben qué día es y suelen inventarlo |

Se aplican a **todas** las peticiones (chat del panel, API y aplicaciones como Open WebUI). Lo que envía el cliente
siempre manda, y el prompt de sistema solo se añade si la conversación no trae uno. La fecha y hora se añaden
siempre (también si el cliente trae su propio prompt de sistema), salvo que se desactiven.

## Modelos multimodales (visión y audio)

Los modelos con visión o audio (Gemma 3/4, Qwen2.5-VL, Qwen2.5/3-Omni, Pixtral, Voxtral…, según
[la lista de llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)) necesitan un segundo
fichero, el **proyector** (`mmproj-*.gguf`). DLLM lo gestiona solo:

- **Hugging Face:** al descargar un modelo, si el repositorio tiene proyector, se descarga con él (se prefiere F16).
- **Ollama:** se descarga su capa de proyector.
- **A mano:** si hay un `mmproj-*.gguf` en la misma carpeta que el modelo, se detecta.
- **Al cargar**, se activa (`--mmproj`) y el chat del panel muestra **📎 Adjuntar** para enviar una imagen o un
  audio (wav/mp3) si el modelo admite audio, como Gemma 4 E2B/E4B.

Por API se usa el formato de OpenAI: partes `{"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}`
o `{"type": "input_audio", "input_audio": {"data": "<base64>", "format": "wav"}}` en `content`.

El proyector se ejecuta en el **orquestador, en CPU** (`--no-mmproj-offload`), no se reparte entre los nodos: cuenta
en la RAM del orquestador (cientos de MB) y procesar una imagen tarda unos segundos. Para usar un modelo multimodal
solo como texto, elige *Ninguno* en su proyector.

## Línea de comandos

| Comando | Para qué |
|---|---|
| `dllm setup` | Asistente de instalación (navegador o `--tui`) |
| `dllm open` | Abre el panel (arranca DLLM si está parado; con `--hide-console` esconde la consola) |
| `dllm pair` | En un nodo instalado sin código: confirma el orquestador que lo reclama |
| `dllm run` | Arranca lo que diga la configuración (lo usa el arranque automático) |
| `dllm status` | Estado, nodos y reparto actual |
| `dllm plan MODELO [--ctx N]` | Simula el reparto sin cargar nada |
| `dllm load MODELO [--ctx N]` | Carga el modelo (asíncrono; sigue con `status`) |
| `dllm unload` | Descarga el modelo y libera los nodos |
| `dllm join-code` | Muestra el código de unión (orquestador) |
| `dllm doctor` | Diagnóstico del equipo local |
| `dllm backup export\|import\|inspect [FICHERO]` | Copia de seguridad completa (cifrada por defecto) y restauración |
| `dllm uninstall [--gui\|--interactive] [--purge] [--models]` | Desinstala: programa, arranque automático y cortafuegos; con `--purge` también llama.cpp y la configuración; con `--models` los modelos de la carpeta de DLLM |

`MODELO` es la ruta relativa a la carpeta de modelos, p. ej. `qwen/qwen2.5-32b-instruct-q4_k_m.gguf`.
`status/plan/load/unload` hablan con `http://127.0.0.1:8080` usando la API key de la configuración local; para otro
orquestador: `--url http://IP:8080 --api-key CLAVE` (o la variable `DLLM_API_KEY`).

Ejemplo de `dllm plan`:
```
modelo qwen2.5-32b · ctx 4096 · necesita 20.7 GiB (KV 1.0 GiB)
  → pc-gamer            10.9 GiB   52.7%  34 capas  [gpu, ~300 GB/s, 941 Mbps, 0.3 ms]
  → mini-pc              9.8 GiB   47.3%  30 capas  [cpu, ~38 GB/s, 938 Mbps, 0.4 ms]
  ✗ portatil-wifi: red 180 Mbps < mínimo 800 Mbps
  ≈ 3.1 tok/s (estimación gruesa)
```

**Contexto (`--ctx`)**: es la "memoria de trabajo" del modelo dentro de una conversación. Más contexto = más memoria
(caché KV, repartida entre los nodos) y respuestas algo más lentas. El reparto muestra el **contexto máximo** que cabe
con la memoria que aportan los nodos, con tope en el que admite el modelo. Empieza con 4096 y sube si lo necesitas.

## Gestionar lo que aporta cada nodo

Cada nodo comparte memoria (RAM, o VRAM si usa GPU NVIDIA) e hilos de CPU. Al instalarlo se elige:

- **Automático (recomendado):** lo gestiona el orquestador. En el panel, botón **Configurar** del nodo:
  - *Porcentaje del equipo:* p. ej. 33 % de un equipo con 16 GiB y 12 hilos → ~5,3 GiB y 4 hilos.
  - *Memoria / hilos exactos:* tienen prioridad sobre el porcentaje.
  - *Usar recomendado:* RAM: hasta el 75 % (sin pasar de la libre menos 1 GiB). VRAM de GPU: la libre menos 1 GiB
    (máx. 95 %). Hilos: la mitad.
- **Manual:** los límites se fijan en el propio nodo y el panel solo los muestra. Para cambiarlos, vuelve a ejecutar el
  asistente en ese nodo, o usa `dllm setup --role node --join … --share 33` (o `--mem-gb`, `--threads`).

Los hilos son un límite real (se pasan a `rpc-server`). La memoria la respeta el reparto: el orquestador nunca asigna
a un nodo más de lo que ofrece. El uso de una GPU solo se puede limitar en memoria. Si el nodo está en uso, el cambio
se aplica al recargar el modelo (**Descargar** → **Cargar**).

## API compatible con OpenAI

Base: `http://127.0.0.1:8080/v1`. DLLM reenvía a `llama-server`, así que vale cualquier cliente de OpenAI
(Open WebUI, LibreChat, Continue, LangChain…). El campo `model` elige entre los modelos cargados; si falta o no
coincide con ninguno, responde el principal (ver [Varios modelos](#varios-modelos-a-la-vez)).

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer TU_API_KEY' \
  -d '{"messages":[{"role":"user","content":"Explica qué es un VLAN en dos frases"}],"stream":false}'
```

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="TU_API_KEY")   # cualquier texto si no hay API key
r = c.chat.completions.create(model="local", messages=[{"role": "user", "content": "Hola"}], stream=True)
for chunk in r:
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

Si no hay modelo listo la API responde `503` con el estado actual.

### API de control

| Método y ruta | Cuerpo | Respuesta |
|---|---|---|
| `GET /api/state` | — | Estado completo (nodos, plan, eventos) |
| `GET /api/models` | — | `.gguf` disponibles |
| `GET /api/resources` | — | VRAM, RAM y núcleos por nodo y totales, con lo reservado por los modelos cargados |
| `GET /api/events` | `?level=info\|warn\|error&slot=N&node=NOMBRE&q=texto&since=T&limit=300` | Eventos filtrados y `counts` por nivel |
| `POST /api/plan` | `{"model": "...", "ctx": 4096, "slot": 0\|"new", "targets": [{"node": "DSH", "device": "cpu"}]}` | Plan sin cargar, con `resources` (clúster tras cargarlo). `targets` vacío = automático; `device`: `gpu` o `cpu`; `@host` = RAM del orquestador |
| `POST /api/load` | `{"model": "...", "ctx": 4096, "slot": 0\|N\|"new", "pinned": true, "targets": […]}` | `202` con el `slot` usado (la carga sigue en segundo plano) |
| `POST /api/unload` | `{"slot": N}` (opcional; sin `slot` descarga el principal) | `200`; en cualquier estado, y **cancela** una carga en curso sin esperar |
| `GET /api/hf/search?q=…` · `GET /api/hf/files?repo=…` | — | Búsqueda de modelos GGUF y ficheros de un repo |
| `POST /api/downloads` | `{"source":"hf","repo":…,"file":…}` · `{"source":"ollama","name":"qwen2.5:7b"}` · `{"source":"ollama-local","name":…}` | `202` con la descarga |
| `GET /api/downloads` · `POST /api/downloads/cancel` | — · `{"id": "d1"}` | Descargas y progreso · cancelar |
| `POST /api/downloads/clear` | `{"id": "d1"}` (sin `id`: todas las terminadas) | Borra del historial las descargas terminadas (no los ficheros) |
| `GET /api/ollama/local` | — | Modelos de la instalación local de Ollama |
| `POST /api/models/settings` | `{"model": …, "settings": {"temperature": 0.7, …}}` (`{}` = restablecer) | Parámetros guardados |
| `POST /api/models/delete` | `{"model": …}` | `409` si está cargado |
| `GET/POST /api/hf/token` | `{"token": "hf_…"}` (vacío = borrar) | `{"set": true/false}` |
| `POST /api/node/settings` | `{"node": "IP:8765", "enabled": true, "layers": 20, "bw_gbps": 360}` (`null` = automático) | Ajustes del nodo en el orquestador |
| `POST /api/node/settings` | `{"node": "IP:8765", "allow_slow": true, "confirm": true}` | Modo forzado para una red lenta; sin `confirm: true` responde `400` |
| `POST /api/nodes/add` | `{"host": "192.168.1.25", "port": 8765}` | `{"state": "añadido"\|"reclamando", ...}`; con `reclamando` incluye `sas` |
| `GET\|POST /api/nodes/scan` | `{"range": "192.168.1.0/24", "port": 8765}` (opcional) | Lanza la búsqueda / estado y `found` (`libre`, `en_cluster`, `otro`) |
| `GET\|POST /api/nodes/bench` | `{"nodes": ["IP:8765"]}` (opcional; vacío = todos) | Lanza la medición uno a uno y en conjunto / estado y resultados |
| `POST /api/nodes/forget` | `{"node": "IP:8765"}` | Elimina el nodo de la lista; `409` si lo usa un modelo |
| `GET /api/telemetry[?since=t]` | — | Estado en tiempo real: tokens/s, peticiones y, por nodo, GPU/CPU/RAM/tráfico con historial |
| `POST /api/node/limits` | `{"node": "IP:8765", "share": 33}` o `"mem_gb"`/`"threads"`/`"device"` (`""` = automático), o `{"node": …, "reset": true}` | Límites efectivos; `400` con motivo si el nodo es manual o el valor no es válido |
| `GET /metrics` | — | Métricas Prometheus |
| `GET /api/access` | — | Si la petición es local, si hace falta clave y (en local) el enlace para otros equipos |
| `POST /api/pair/claim` | `{"host": "IP", "port": 8765}` (`port` solo si no está en `pending`) | Reclama un equipo de `pending` (en `/api/state`); devuelve el código de verificación |
| `POST /api/ui/ping?id=…` · `/api/ui/bye?id=…` · `GET /api/ui/clients` | — | Pestañas del panel abiertas (lo usa `dllm open`) |
| `GET /healthz` | — | `200` siempre que el proceso esté vivo (sin API key) |

Todas, salvo `/` y `/healthz`, exigen `Authorization: Bearer API_KEY` si hay API key configurada y la petición no
viene del propio equipo (127.0.0.1).

## Métricas (Prometheus / Zabbix)

`GET /metrics` expone: `dllm_state{state}`, `dllm_node_up{node}`, `dllm_node_cap_bytes{node}`,
`dllm_node_net_mbps{node}`, `dllm_recoveries_total`, `dllm_requests_total`, `dllm_request_errors_total` y, por modelo,
`dllm_slot_requests_total{slot,model}`, `dllm_slot_errors_total`, `dllm_slot_latency_p95_ms`,
`dllm_slot_tokens_per_second`, `dllm_slot_active_requests`, `dllm_slot_recoveries_total`; por nodo,
`dllm_node_rpc_restarts_total`, `dllm_node_ram_free_bytes`, `dllm_node_vram_reserved_bytes`, y `dllm_events_total{level}`.

- **Prometheus**: `scrape_configs: [{job_name: dllm, static_configs: [{targets: ['IP:8080']}], authorization: {credentials: API_KEY}}]`
- **Zabbix**: ítem *HTTP agent* a `http://IP:8080/metrics` con cabecera `Authorization: Bearer API_KEY` y ítems
  dependientes con preprocesado *Prometheus pattern*, p. ej. `dllm_state{state="READY"}` y
  `dllm_node_up{node="mini-pc"}`. Disparadores útiles: el ítem de `READY` a 0 durante 5 min, un `dllm_node_up` a 0,
  o `change()` de `dllm_recoveries_total` mayor que 0.

## Recuperación automática

Si un nodo **en uso** deja de responder (10 s) o `llama-server` termina, el orquestador replanifica con los nodos que
quedan y vuelve a cargar el modelo (estado `RECOVERING`). Si ya no cabe, queda en `ERROR` y lo reintenta solo cuando
cambie el conjunto de nodos (p. ej. cuando el equipo caído vuelva). Las peticiones en curso durante la caída fallan.

## Consejos de rendimiento

- Cable y mismo switch. Evita Wi-Fi y saltos entre subredes.
- Menos nodos es mejor: si un modelo cabe en dos, no añadas un tercero.
- Pon los modelos en SSD del orquestador: la carga envía los pesos a cada nodo por la red (un 30 GB a 1 Gbps ≈ 4–5 min).
- Si el reparto no refleja la realidad (p. ej. una CPU que rinde más de lo estimado), ajusta en el asistente
  *ancho de banda* (`--bw-gbps`) o *memoria a ofrecer* (`--mem-gb`).
- Cuantizaciones Q4_K_M/Q5_K_M dan el mejor equilibrio calidad/tamaño.

### GPU local directa

Si un modelo cabe entero en la GPU de un nodo **de este mismo equipo**, DLLM lo lanza ahí sin RPC ni túnel
(`llama-server --device <GPU>`, sin `--rpc`): menos latencia y sin pasar por la red. Esa GPU queda reservada en el
libro de recursos, pero **el resto de nodos y la RAM siguen libres** para cargar otros modelos a la vez (p. ej. uno
pequeño en la GPU local y uno grande repartido entre los demás). El slot muestra la etiqueta *directa*. Si no encaja o
no se puede identificar la GPU, usa el reparto RPC de siempre.

**Tú decides dónde se usa**, en tres niveles (el más específico gana):

| Nivel | Dónde | Efecto |
|---|---|---|
| Modelo | Modelos → Parámetros → *GPU local directa* (`local_gpu`) | `auto` (por defecto): según el host · `off`: **nunca** directo, siempre RPC aunque quepa · `on`: **obliga**; si no es posible (destino en CPU, nodo remoto, GPU no visible) la carga falla con el motivo |
| Host | Nodos → Configurar → *Permitir ejecución directa* (`direct_local`, API `/api/node/settings`) | Desmarcado: ese equipo nunca ejecuta en directo (los modelos con `auto` irán por RPC). El valor por defecto es permitido |
| Global | `--no-direct-local` al arrancar | Desactiva el directo para todos los modelos en `auto` |

Cada decisión que descarta el modo directo queda anotada en *Registros*. Para no usar un equipo en absoluto, desmárcalo con
*Usar este nodo en los repartos*; para fijar dónde corre un modelo, usa *destinos* (`targets`) al cargarlo.

## Conectar front-ends y herramientas

La pestaña **Conectar** da la URL base, la API key y fragmentos listos para AnythingLLM, Open WebUI, curl y Python; la
lista de modelos (`/v1/models`) incluye sus capacidades (`tools`, `vision`, `embeddings`) y **Probar** hace una llamada
real de función para comprobarlo. DLLM arranca `llama-server` con `--jinja` si la plantilla del modelo admite
herramientas (ajuste `tools`: `auto`/`on`/`off`), de modo que `tools`/`tool_calls` del protocolo OpenAI funcionan tal
cual; **no ejecuta las herramientas**: lo hace el front-end. Ver [INTEGRACIONES.md](INTEGRACIONES.md).

## Copia de seguridad y recuperación

*Sistema* → **Crear copia** descarga un único fichero `.dllmbak` (ZIP con sha256 por fichero; cifrado con
PBKDF2-HMAC-SHA256 + HMAC, solo biblioteca estándar) con: secreto del clúster, API key, ajustes de modelos y nodos, nodos
añadidos por IP, token de Hugging Face y la lista de modelos instalados con su origen. Los modelos no se incluyen (se
re-descargan). **Restaurar copia** muestra qué contiene y qué cambiaría; antes de aplicarla guarda el estado actual en
`~/.dllm/backups/` (conserva 5). Equivalente en terminal: `dllm backup export|inspect|import`. Si la restauración cambia
el secreto o la API key, pide reiniciar. En un equipo nuevo: instalador → *Restaurar desde una copia*.

### Varios modelos a la vez

Cada modelo cargado ocupa un *slot* (hasta 8) con su propio `llama-server` y sus `rpc-server`; la memoria de cada nodo se
reparte entre ellos con un libro de reservas (`ledger` en `/api/state`). Ejemplos:

```bash
dllm load qwen2.5-32b-q4.gguf                         # slot 0, fijado
dllm load nomic-embed.gguf --slot new --on-demand      # slot nuevo, expulsable por LRU
curl http://127.0.0.1:8080/v1/embeddings -d '{"model":"nomic-embed.gguf","input":"hola"}' -H 'Content-Type: application/json'
```

- El proxy `/v1/*` enruta por el campo `model` (también en formularios multipart) y por la capacidad de la ruta:
  un modelo con el ajuste `embeddings: true` atiende `/v1/embeddings` y no `/v1/chat/completions` (y al revés → `400`).
  Con un nombre desconocido o sin `model`, responde el modelo principal.
- `placement` (ajuste del modelo): `split` reparte entre nodos; `node` lo fija a un único nodo; `auto` (por defecto) usa
  `node` para embeddings y `split` para el resto.
- `device_policy` (ajuste del modelo): `auto`, `gpu_strict`, `cpu_only` (en la RAM del orquestador, sin VRAM),
  `gpu_with_cpu_fallback` (las capas que quepan en GPU y el resto en CPU). Márgenes: `--vram-safety-mb`, `--ram-safety-mb`.
- Con memoria ocupada por modelos en uso, la petición espera hasta `--queue-timeout` s y recibe `429` (`Retry-After`);
  si el modelo no cabe ni sin los no fijados, `503` inmediato. Errores en formato OpenAI.
- `dllm up --on-demand`: si `model` es un `.gguf` de la carpeta de modelos y no está cargado, se carga al recibir la
  petición (la petición espera) y, si falta memoria, se expulsan los modelos no fijados que llevan más tiempo sin usarse.
  Los fijados (`pinned`, el valor por defecto de `/api/load`) nunca se expulsan.
