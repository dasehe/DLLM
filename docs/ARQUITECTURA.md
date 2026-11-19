# Arquitectura

## Componentes

```
┌────────────────────────── ORQUESTADOR ───────────────────────────┐        ┌──────────── NODO ─────────────┐
│ HTTP :8080  panel · /api · /v1 (proxy) · /metrics                 │        │ HTTP :8765  API firmada HMAC   │
│   │                                                               │        │   /info /rpc/* /limits         │
│   ├─ Registry  beacons UDP :50999 firmados + sondeo /info (3 s) ──┼──────► │   /bench/*                     │
│   ├─ Scheduler lee GGUF, mide red, decide reparto                 │        │                                │
│   ├─ Cluster   ciclo de vida, vigilancia y recuperación           │        │ TCP :50051  proxy RPC          │
│   │     │                                                         │        │   handshake HMAC mutuo         │
│   │     └─ llama-server (127.0.0.1) --rpc 127.0.0.1:<túnel i>     │        │        │                       │
│   │                        │                                      │        │        ▼                       │
│   └─ Tunnel i ◄────────────┘  handshake HMAC ─────────────────────┼──────► │ rpc-server (solo 127.0.0.1)    │
└───────────────────────────────────────────────────────────────────┘        └────────────────────────────────┘
```

| Módulo | Responsabilidad |
|---|---|
| `dllm/agent.py` | Agente de nodo: lanza/vigila `rpc-server`, proxy autenticado, beacons, benchmark, límites |
| `dllm/orchestrator.py` | Registro de nodos, planificación, ciclo de vida de `llama-server`, API, métricas |
| `dllm/scheduler.py` | Modelo de coste, reparto y contexto máximo |
| `dllm/models.py` | Catálogo de modelos, descargas (Hugging Face, Ollama, Ollama local), proyectores multimodales y parámetros por modelo |
| `dllm/telemetry.py` | Telemetría del nodo: GPU (nvidia-smi, amdgpu sysfs, contadores WDDM de Windows, ioreg), CPU, RAM, tráfico RPC |
| `dllm/limits.py` | Cuánto aporta cada nodo (memoria, hilos, modo auto/manual) |
| `dllm/gguf.py` | Lectura de metadatos GGUF (capas, cabezas, contexto, tamaño, modelos troceados) |
| `dllm/hw.py` | Memoria, GPU NVIDIA, ancho de banda de memoria, búsqueda de binarios |
| `dllm/engine.py` · `ledger.py` | Drivers de motor, enrutado por capacidad, libro de memoria por slot y nodo |
| `dllm/auth.py` · `util.py` | Firmas HMAC, handshake, túnel TCP |
| `dllm/installer.py` · `wizard.py` · `config.py` | Instalador, asistente, configuración y código de unión |
| `dllm/ui.html` · `setup.html` | Panel y asistente web (HTML estático, sin dependencias) |

Solo biblioteca estándar de Python; el cómputo lo hace `llama.cpp`.

## Qué viaja por la red (y qué no)

El modelo no se trocea en "tareas" que se envían una y otra vez. Funciona así:

1. **Al cargar**, el orquestador lee el `.gguf` y envía a cada nodo **sus capas** (los pesos). Esto ocurre una vez y
   es lo lento: un modelo de 30 GB a 1 Gbps tarda 4–5 minutos.
2. **Cada nodo guarda sus capas en su RAM/VRAM y además la caché de contexto (KV) de esas capas.**
3. **Por cada token**, solo viaja el vector de activaciones de una capa a la siguiente (unos KB): orquestador →
   nodo 1 → nodo 2 → … → orquestador. Cada nodo calcula sus capas con sus datos locales.

Consecuencias:

- **Más memoria en el clúster = más contexto posible.** La caché KV crece con el contexto y se reparte
  entre los nodos junto con sus capas. En modelos con **atención de ventana deslizante** (Gemma 2/3/4, Cohere 2…) las
  capas SWA solo guardan la ventana, así que el contexto cabe en mucha menos memoria; el cálculo lo tiene en cuenta
  (patrón por capa, cabezas KV y dimensiones por capa, capas con caché compartida), igual que llama.cpp. El reparto muestra el **contexto máximo** que cabe con la memoria que aportan
  los nodos (`max_ctx`), con tope en el contexto con el que se entrenó el modelo: pasarse de ese tope degrada la calidad.
- **Un orquestador rápido apenas cambia la velocidad.** Mandan la latencia de red por salto (cada token recorre todos
  los nodos) y el ancho de banda de memoria de cada nodo.
- **Más contexto no es gratis.** Procesar un prompt largo cuesta cómputo, sobre todo en CPU, y cada token lee la caché
  KV completa, así que con contextos grandes cada respuesta tarda más.
- "Recordar" aquí significa **memoria de trabajo dentro de una conversación**, no memoria entre conversaciones.

## Autoconfiguración del agente

El objetivo es que instalar el agente baste. Al arrancar, y tras cada actualización:

1. **Detecta los dispositivos preguntando a llama.cpp:** `rpc-server -d <inexistente>` lista lo que ve con su
   memoria (`Vulkan0: AMD Radeon RX 7600 XT (16368 MiB, 15800 MiB free)`). Vale igual para NVIDIA (CUDA),
   AMD/Intel (Vulkan, ROCm) y Apple (Metal). Módulo `dllm/devices.py`.
2. **Elige un dispositivo:** el acelerador con más memoria, o el indicado con `--device` (nombre o parte de la
   descripción). Lo fija con `rpc-server -d`, porque por defecto rpc-server expone **todos** los aceleradores. El backend
   Vulkan ya descarta la gráfica integrada si hay una dedicada.
3. **Informa de su memoria real** (VRAM total y libre, que vuelve a medir cada minuto mientras no está en uso).
   Sin acelerador, presta CPU y RAM.

**Misma versión en todo el clúster:** el protocolo RPC exige la misma build de llama.cpp. Si el orquestador ve un
nodo con otra build, le pide (`POST /llama/install`, firmado) que instale la suya. El agente descarga el paquete
oficial para su sistema y backend (el que se instaló, `--llama-backend`), verifica el sha256, cambia de binarios,
vuelve a detectar dispositivos y lo guarda en su configuración. Como mucho se intenta una vez cada 15 min por nodo. Se
desactiva con `--no-auto-update` en el nodo o `--no-auto-sync` en el orquestador.

**Dispositivos en el orquestador:** antes de lanzar `llama-server` se listan sus dispositivos sin y con `--rpc …`. La
diferencia son los remotos, en orden, y se lanza con `--device <solo esos>` y un `-ts` alineado. Así la GPU local del
orquestador no entra en el reparto (rol *Ambos* en un PC con GPU), y no se depende de cómo nombre llama.cpp a los
dispositivos RPC. Si un nodo expusiera más de uno, la carga falla con un mensaje claro. Además se avisa si a un nodo
se le asigna más de lo que su dispositivo tiene libre.

## Cuánto aporta cada nodo

Módulo `dllm/limits.py`. Cada nodo ofrece **memoria** (RAM, o VRAM si usa GPU NVIDIA) e **hilos de CPU**.

| Modo (se elige al instalar el nodo) | Quién decide | Panel del orquestador |
|---|---|---|
| **Automático** (por defecto) | El orquestador. Empieza con el valor recomendado | Botón **Ajustar** en el nodo |
| **Manual** | El propio nodo (asistente o `--share/--mem-gb/--threads`) | Solo lectura («fijado en el nodo») |

Valores (prioridad: valor exacto > porcentaje > recomendado):

- **Recomendado:** memoria = el mínimo entre (libre − reserva) y el 75 % del total en RAM o el 95 % en VRAM; hilos = la mitad de los hilos
  lógicos (≈ núcleos físicos, lo que mejor rinde en llama.cpp).
- **Porcentaje (`share`):** se aplica a la memoria total y a los hilos. Ejemplo: 16 GiB y 6C/12T al 33 % → ~5,3 GiB y
  4 hilos.
- **Tope:** nunca más del total menos la reserva (1 GiB por defecto) ni más hilos de los que tiene el equipo. Si se
  pide más, se recorta y el panel lo indica.

Cómo se hacen cumplir: los **hilos** se pasan a `rpc-server -t N`, un límite real de CPU. La **memoria** no tiene
opción en `rpc-server`, así que la respeta el planificador: nunca asigna a un nodo más capas y caché KV de lo que
ofrece. Una GPU solo se puede limitar en memoria, no en % de cómputo. El ancho de banda estimado de un nodo CPU baja
proporcionalmente si se le dan menos hilos que sus núcleos físicos.

Los cambios hechos desde el panel se guardan en el `config.json` del nodo y sobreviven a reinicios. Si el nodo está
en uso, la memoria nueva cuenta en el siguiente reparto y los hilos al recargar el modelo.

## Flujo de carga

1. `POST /api/load` → el worker toma el turno (una carga a la vez; una nueva carga cancela la anterior).
2. Lee el GGUF y calcula memoria necesaria: pesos + caché KV (por capa con caché propia: `celdas·n_head_kv·(dim_k+dim_v)·bytes`, con celdas = contexto
   en capas globales y ventana + 512 en capas SWA) + 5 %.
3. Mide **secuencialmente** cada nodo vivo: RTT (mínimo de 5 pings) y 16 MiB de subida y bajada. En paralelo se
   repartirían el enlace del orquestador y todos parecerían más lentos.
4. Excluye nodos: sin respuesta, sin `rpc-server`, **build de llama.cpp distinta** (el protocolo RPC no es compatible
   entre versiones), red < `min_mbps`, memoria insuficiente.
5. Scheduler → nodos elegidos, `--tensor-split` y estimación de tok/s.
6. Pide a cada nodo `/rpc/start`, abre un túnel local por nodo y lanza
   `llama-server -m MODELO --rpc 127.0.0.1:T1,127.0.0.1:T2… -ts s1,s2… -ngl 999 -c CTX`.
7. Espera a `/health` de `llama-server` → `READY`.

## Scheduler

Inferencia en pipeline por capas, un stream, limitada por memoria:

```
t_token ≈ Σ_i (bytes_i / (bw_i · 0,65))  +  Σ_i rtt_i  +  3 ms
```

Minimizar esa suma con `Σ bytes_i = M` y `bytes_i ≤ capacidad_i` se resuelve **llenando primero los nodos con mayor
ancho de banda de memoria** y usando **el mínimo de nodos** (cada nodo extra añade latencia). Las capas se asignan
proporcionalmente con el método del resto mayor. Margen por nodo: 512 MiB + 5 % libre.

### Slots de modelo y libro de memoria

Cada modelo cargado ocupa un *slot* (0-7). El slot es un `Cluster` con su `llama-server` (puerto interno efímero en
127.0.0.1) y, en cada nodo que use, su `rpc-server` propio (puerto local efímero) detrás del mismo proxy autenticado del
agente: el cliente indica el slot en la etiqueta del handshake HMAC (`c` + número; el slot 0 usa `c`, igual que 0.4.x),
de modo que cada slot tiene su canal independiente sin abrir puertos nuevos. `ledger.Ledger` lleva las reservas por
slot y nodo: disponible = aporte − reservas de los otros slots − margen (256 MiB si ya hay otro slot en el nodo), y
«planificar + reservar» se hace bajo un cerrojo común. Un agente anterior a 0.5.0 no admite slots > 0 (se excluye).

### Política de dispositivo, memoria y cola

`device_policy` (ajuste del modelo): `auto` (por defecto: todos los nodos, `-ngl 999`), `gpu_strict`, `cpu_only` y
`gpu_with_cpu_fallback` (ver CHANGELOG). Con `cpu_only` y con la parte en CPU del *fallback*, llama-server usa la CPU
del equipo del orquestador y la memoria se reserva en `@host` (RAM libre real − margen − reservas). El ledger tiene dos
dimensiones (`vram`, `ram`) y márgenes configurables. Un slot está `IDLE` o `BUSY` (`active_requests > 0`); el LRU solo
toca slots no fijados e `IDLE`; si todos los candidatos están `BUSY`, la petición espera en una cola acotada por
`--queue-timeout` y luego recibe `429` + `Retry-After`; un modelo que no cabe ni sin los no fijados recibe `503` antes de
lanzar nada. En un nodo solo-CPU lo reservado es RAM; en uno con GPU, VRAM más 256 MiB de RAM para buffers de E/S.

### Motores, enrutado L7 y bajo demanda

`engine.EngineDriver` desacopla el slot del proceso: capacidades, endpoints, comando, comprobación de salud, estimación
de memoria y colocación por defecto (`split` entre nodos por RPC, o `node`, fijado a un único nodo y GPU: *Task
Affinity*, p. ej. embeddings). El proxy `/v1/*` mira el campo `model`, busca el slot cuyo modelo coincide y comprueba que
su driver admite la capacidad de la ruta (`engine.OPENAI_ROUTES`); si no hay coincidencia, atiende el modelo principal.
Con `--on-demand`, un `.gguf` de la carpeta de modelos se carga en un slot nuevo (sincronizado con un cerrojo, con la
sonda de plan previa) y, si falta memoria, se expulsan por LRU los slots `pinned: false` listos y sin peticiones en curso
hasta que quepa. El proxy no almacena en búfer: SSE y respuestas binarias se reenvían tal cual. La petición que provoca la
carga espera a que termine (puede tardar minutos; configura el tiempo de espera del cliente).

### Topología de red (tiers)

El orquestador mide cada enlace antes de planificar (`util.probe_link`: ecos de 1 byte para el RTT y bloques de 4 MiB
en cada sentido, por el puerto del proxy del agente y con el mismo handshake HMAC mutuo que el túnel RPC, pero en
modo «probe», que no toca `rpc-server`) y lo clasifica (`scheduler.classify`):

| Tier | Enlace | Criterio |
|---|---|---|
| 0 | mismo equipo | host del nodo = loopback o IP propia |
| 1 | LAN rápida | RTT < 1 ms y ≥ 2000 Mbps medidos |
| 2 | LAN | el resto con RTT ≤ 10 ms (incluye 1 GbE y el hueco 1–10 ms) |
| 3 | Wi-Fi / VPN / WAN | RTT > 10 ms |

Entre los repartos que caben (búsqueda exhaustiva hasta 12 nodos; voraz por encima) se elige: **menor tier máximo →
menos nodos remotos → menor tiempo por token → menos nodos**. Los nodos se llenan por tier ascendente, así que el más
lejano lleva menos capas. Un nodo de tier 3 solo entra si el modelo no cabe sin él (y el plan avisa). Los umbrales son
constantes de `scheduler.py` (`TIER1_MBPS`, `TIER1_RTT_MS`, `TIER3_RTT_MS`). Limitación: el ancho de banda se mide con
un solo flujo TCP en Python; un enlace de 2,5 GbE puede quedar por debajo de `TIER1_MBPS` y contarse como tier 2.

`bw` de los nodos: GPU NVIDIA 300 GB/s por defecto; CPU medido con un `memcpy` de un hilo × min(núcleos, 2). Son
**heurísticas para ordenar**, no un benchmark: se pueden fijar con `--bw-gbps`.

## Descubrimiento y vida de los nodos

- El agente envía cada 3 s un beacon UDP firmado (`nombre`, puertos, hora) a `announce_to` (el orquestador, tomado del
  código de unión) o, si no hay, a broadcast.
- El orquestador sondea `/info` de cada nodo cada 3 s; un nodo sin respuesta 10 s se considera caído; uno descubierto
  por beacon que lleva 10 min mudo se olvida.
- Vigilancia del clúster (1 s): nodo en uso caído o `llama-server` muerto → `RECOVERING` y replanificación.
- El agente relanza `rpc-server` si muere mientras está activo.
- **Nodo sin código de unión**: anuncia `DLLMPAIR1` (sin firma) por broadcast; el orquestador lo lista en `pending`
  (30 s de vida) y, al reclamarlo, hace el emparejamiento de `pairing.py` (DH, código de verificación aceptado en el
  nodo, secreto cifrado). El agente cambia a modo normal en caliente y guarda el secreto en su `config.json`.
- Todas las respuestas HTTP del agente y del orquestador usan `TCP_NODELAY`: sin él, las cabeceras y el cuerpo salían
  en dos envíos y el segundo esperaba al ACK retardado del otro equipo (~40 ms de RTT medido entre equipos).

## Modelos y descargas

- **Hugging Face:** `GET /api/models?search=…&filter=gguf`, árbol de ficheros `GET /api/models/{repo}/tree/main`
  (sha256 en `lfs.oid`), descarga de `/{repo}/resolve/main/{fichero}`.
- **Ollama:** manifiesto `registry.ollama.ai/v2/{ns}/{modelo}/manifests/{tag}`. La capa
  `application/vnd.ollama.image.model` es el GGUF (su digest es el sha256); además se leen `…params`, `…system` y
  `…projector`. **Ollama local:** `~/.ollama/models/manifests/…` y `blobs/sha256-…` (o `OLLAMA_MODELS`).
- **Cola de descargas:** una a la vez, en un fichero `.part`, con reanudación por `Range`, comprobación de espacio libre
  (fichero + 1 GiB), sha256 y cabecera GGUF antes de darla por buena.
- **Parámetros por modelo:** `~/.dllm/models.json` en el orquestador. Se inyectan en cada petición `/v1/chat/completions`
  y `/v1/completions` sin pisar los del cliente. El token de Hugging Face está en `~/.dllm/secrets.json` (600).
- **Multimodal:** proyector elegido o detectado en la carpeta del modelo → `llama-server --mmproj … --no-mmproj-offload`.

## Telemetría y ajustes por nodo

- **Agente:** un hilo muestrea cada 2 s (3 s en Windows) la GPU que presta, la CPU, la RAM y los bytes que pasan por
  su proxy RPC, y los publica en `/info` (`telemetry`). Para la GPU usa, por orden: `nvidia-smi`; `sysfs` de amdgpu
  (`gpu_busy_percent`, `mem_info_vram_*`, hwmon `temp1_input` en m°C, `power1_average`/`power1_input` y
  `power1_cap` en µW); en Windows, la API PDH con los contadores del Administrador de tareas («GPU Engine» → uso del
  motor más ocupado, «GPU Adapter Memory» → VRAM dedicada), con nombres en inglés para que funcione en cualquier
  idioma; `ioreg` en macOS. Si nada está disponible, la memoria que informa llama.cpp.
- **Orquestador:** guarda ~5 min de historial por nodo; arranca `llama-server` con `--metrics` y lee su `/metrics`
  cada 2 s para calcular tokens/s de generación y de prompt y peticiones en curso. `GET /api/telemetry` sirve todo al
  monitor.
- **Ajustes por nodo** en `~/.dllm/nodes.json` del orquestador (por nombre de nodo): activado, capas fijas y ancho de
  banda. El planificador asigna primero las capas fijas, comprueba que caben y reparte el resto como siempre.
- **Ajustes de carga por modelo** → opciones de `llama-server`: `-ctk/-ctv` (tipo de caché), `-fa on|off|auto`,
  `-b/-ub`, `-np`, `--rope-scaling/--rope-freq-base/--rope-freq-scale`. El tamaño de la caché KV del reparto usa el
  tipo elegido (bytes por elemento: f16 = 2, q8_0 = 34/32, q4_0 = 18/32…). `--mlock`/`--no-mmap` no se exponen
  porque las versiones recientes de llama.cpp los sustituyeron por `--load-mode`.

## Puertos

| Puerto | Proto | Dónde | Quién se conecta |
|---|---|---|---|
| 8080 | TCP | Orquestador | Usuarios / aplicaciones (solo loopback salvo `--expose`) |
| 50999 | UDP | Orquestador | Beacons de los nodos |
| 8765 | TCP | Nodo | Orquestador (API de control) |
| 50051 | TCP | Nodo | Orquestador (datos del modelo) |
| efímeros | TCP | 127.0.0.1 | `llama-server` interno, túneles, `rpc-server` |

## Instalador

- Elige la **compilación `bNNNN` más reciente que publica todos los paquetes habituales** (Windows CPU/Vulkan/CUDA,
  Linux CPU/Vulkan, macOS arm64). Ignora borradores y etiquetas que no son compilaciones, como `v1.0.0`,
  que llama.cpp marca como «Latest» y no trae binarios (las `bNNNN` se aceptan aunque estén marcadas como
  pre-release, que es como las publica llama.cpp). Así todos los nodos encuentran su paquete en la misma versión
  que el orquestador.
- Selecciona el paquete de [releases de llama.cpp](https://github.com/ggml-org/llama.cpp/releases) por sistema,
  arquitectura y backend: `ubuntu[-vulkan|-rocm-X|-sycl-fp16|-cuda-X]-ARCH` (+ `cudart`), `macos-ARCH`,
  `win-cpu-ARCH` + superposición `win-vulkan|win-rocm-X|win-sycl|win-cuda-X` (+ `cudart`). Para CUDA, la versión más
  alta que admite el driver. Con backend *auto*, si uno falla prueba el siguiente (CUDA → Vulkan → CPU).
- Verifica el `sha256` publicado por la API de GitHub; extrae rechazando rutas fuera del destino y enlaces que escapen.
- Comprueba que los binarios arrancan (`--version`) y da pistas si faltan librerías (Vulkan, libgomp).
- Servicio de usuario: systemd `--user` (Linux), LaunchAgent (macOS), tarea programada al iniciar sesión (Windows).
- Código de unión `DLLM1-<base64url(json)>-<crc32>`: `secret`, `orch` (IP), `beacon` (puerto), `tag` (build
  llama.cpp), `name`. El CRC detecta errores al copiar, no aporta seguridad.

## Límites

- **No acelera un stream**: más nodos = modelos mayores, no más tokens/s.
- **Sin cifrado** del tráfico (ver [SEGURIDAD](SEGURIDAD.md)).
- Un dispositivo por agente (un `rpc-server`): en un equipo con varias GPUs se presta la de más memoria (o la elegida
  con `--device`). El orquestador no aporta cómputo salvo con el rol *Ambos*.
- Se asume que llama.cpp enumera los dispositivos remotos en el orden de `--rpc` (uno por nodo).
- La carga envía los pesos por el túnel Python (cuello de botella ≈ red). `--cache` en el agente activa la caché de
  tensores de `rpc-server` si la build la soporta.
- Las peticiones en curso fallan durante una recuperación.
- El proyector multimodal se ejecuta en la CPU del orquestador, no se reparte. Algunos modelos de Ollama empaquetados
  para su motor propio no los carga llama.cpp.
- La integración con Hugging Face y Ollama está probada contra servidores simulados que reproducen sus APIs; no se ha
  probado aún contra los servicios reales.
- Validado solo con binarios simulados; pendiente de prueba con llama.cpp real y GPU.


## Destinos por modelo, recursos y registros (0.6.0)

- **Destinos** (`targets`): `[{node, device}]` por modelo. `gpu` usa el dispositivo elegido del agente; `cpu` lanza el
  `rpc-server` **de ese modelo** con `-d CPU` (la RAM del nodo), así dos modelos pueden convivir en el mismo nodo, uno en
  su GPU y otro en su CPU, cada uno con su `rpc-server`, su túnel y su reserva en el libro de memoria (VRAM o RAM según
  el dispositivo). El planificador trata un nodo con GPU usado como CPU como un nodo solo-CPU (RAM libre, ancho de banda
  de memoria supuesto de 20 GB/s: la estimación de tok/s es muy gruesa). `@host` es la RAM del orquestador y no se
  combina con nodos. Los destinos se conservan al recuperar un modelo tras una caída.
- **Recursos** (`/api/resources`): se calculan con lo que cada agente informa (VRAM aportada, RAM, núcleos) y el libro de
  memoria; el plan simulado añade su reserva para mostrar el «después» sin cargar nada.
- **Registros** (`dllm/eventlog.py`): cada evento tiene nivel, modelo, nodo y tipo; un evento repetido en menos de 5 s se
  cuenta (`count`) en lugar de duplicarse. Persistencia JSONL con rotación. Métricas por modelo (`SlotMetrics`):
  peticiones, errores por código, media, p50 y p95 de las últimas 300.
- **Cancelar**: descargar un modelo mientras carga lo cancela sin esperar (estado `UNLOADING`) y un modelo secundario
  desaparece cuando termina la limpieza; en estado de error se elimina igual.

## Administración de nodos (0.6.0)

- **Descubrimiento** (`dllm/discovery.py`): además del beacon UDP, el orquestador puede alcanzar un agente por IP. Un
  agente DLLM responde sin autenticación solo en `/pair/*` y solo si no está emparejado; con `GET /pair/status` se
  clasifica `libre` (200) / `emparejado` (404 «ya está emparejado») sin enviar ningún secreto. `add_node` prueba antes
  un `/info` firmado con el secreto del clúster (nodo ya nuestro) y, si no, inicia el emparejamiento. La búsqueda solo
  admite rangos privados de hasta 1024 direcciones y corre en segundo plano (`jobs.scan`).
- **Medición** (`jobs.bench`): `probe_link` de cada nodo en secuencia y después en paralelo; `joint_mbps` y
  `joint_ratio` quedan en `node.net`. Un cociente < 0,6 indica que el enlace del orquestador es el cuello de botella.
- **Modo forzado**: `allow_slow` por nodo (en `nodes.json`) hace que `scheduler.plan` no excluya el nodo por debajo de
  `min_mbps` y añada un aviso. Activarlo exige `confirm: true` en la API.
- **Baja**: `Registry.remove` elimina el nodo y, si seguía anunciándose, ignora sus beacons 10 min (`banned`).
