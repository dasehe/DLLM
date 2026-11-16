# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versionado [SemVer](https://semver.org/lang/es/).

## [Sin publicar]

### Corregido
- **Instalador en Windows con Control inteligente de aplicaciones**: `[WinError 4551]` (y 1260) al lanzar llama.cpp se
  reconoce como bloqueo de directiva y el asistente se detiene con la explicación, en vez de probar otro backend (CPU
  también se bloquea y dejaba un orquestador inservible).

### Documentación
- README: estado real (clúster de 2 equipos Windows probado con llama.cpp real), problema conocido con imágenes en
  modelos grandes repartidos, varios modelos y topología; instalación en Ubuntu desde GitHub (`docs/INSTALACION.md`).
- `LEEME-INSTALAR.md`: Linux con NVIDIA usa el paquete CUDA oficial (antes decía Vulkan). Hoja de ruta al día.

## [0.6.0] - 2026-11-15

### Cambiado (incompatible)
- **LLMM pasa a llamarse DLLM (Distributed LLM)**: paquete `dllm/`, comando `dllm`, `dllm.pyz`, carpeta `~/.dllm`,
  variables `DLLM_*`, métricas `dllm_*`, servicio `dllm-agent`, lanzadores `*-DLLM.*`, código de unión `DLLM1-…`,
  cabeceras `DLLM-*` y marcas de los protocolos de emparejamiento y handshake. Repositorio: `dasehe/dllm`.
- **No hay compatibilidad con 0.5.x**: hay que desinstalar LLMM y reinstalar DLLM en todos los equipos y volver a
  emparejar los nodos. Los modelos y llama.cpp de la carpeta antigua pueden moverse a `~/DLLM` (o usar `--bin-dir`).

## [0.5.1] - 2026-11-15

### Corregido
- **La causa de una caída de `llama-server` ya no se pierde**: antes solo se registraba «llama-server terminó» y su log
  se borraba al relanzar. Ahora *Eventos* muestra el código de salida y las líneas relevantes (`GGML_ASSERT`, error,
  fuera de memoria…), y `/api/state` incluye `last_crash` con la hora, el código y esas líneas.

## [0.5.0] - 2026-11-15

### Añadido
- **Reparto consciente de la topología de red (Fase 1 de 0.5.0)**: cada nodo recibe un *tier* según su enlace con el
  orquestador: 0 mismo equipo · 1 LAN rápida (≥ 2 Gbps y RTT < 1 ms) · 2 LAN (RTT ≤ 10 ms) · 3 Wi-Fi/VPN/WAN
  (RTT > 10 ms). El planificador elige, entre los repartos que caben, el de menor tier máximo, luego el de menos
  saltos de red y luego el de menor tiempo por token estimado. Un nodo de tier 3 solo se usa si el modelo no cabe
  sin él, y entonces el plan lo avisa. Las GPU del propio equipo (tier 0) no cuentan como saltos.
- **Sonda de enlace por el canal autenticado**: antes de planificar, el orquestador mide RTT y ancho de banda (bloques
  de 4 MiB) por el puerto del proxy del agente con el mismo handshake HMAC mutuo del túnel, en modo «probe». Si el
  agente es anterior, usa el bench HTTP de siempre (`via: http`).
- El panel muestra el tier (`T0`–`T3`) junto a Mbps y ms en *Nodos*; el plan incluye `tier`, `max_tier` y `unused`.

- **Slots de modelo y libro de memoria (Fase 2 de 0.5.0, base)**: el orquestador puede tener varios modelos cargados a
  la vez, cada uno en un *slot* con su `llama-server` y, en cada nodo, su propio `rpc-server` y su propio canal
  autenticado (el número de slot viaja en la etiqueta del handshake HMAC: mismo puerto y mismas reglas de cortafuegos;
  el slot 0 es compatible con agentes 0.4.x). El *VRAM Ledger* (`dllm/ledger.py`) resta del aporte de cada nodo lo que
  reservan los demás slots antes de planificar. `POST /api/load` y `/api/unload` aceptan `slot` (número o `"new"`) y
  `pinned`; `/api/state` incluye `slots` y `ledger`.
- **Enrutador L7 por modelo (Fase 3 de 0.5.0)**: `/v1/*` elige el slot por el campo `model` de la petición (JSON o
  formulario multipart; nombre completo, nombre de fichero o sin `.gguf`) y por la capacidad que exige la ruta
  (`chat`, `embeddings`; reservadas `image`, `video`, `stt`, `tts`). SSE y respuestas binarias pasan sin buffering.
  Un nombre desconocido o ausente sigue atendiéndolo el modelo principal. `GET /v1/models` lista los modelos cargados.
- **Carga bajo demanda con LRU** (`--on-demand`, desactivado por defecto): si `model` es un `.gguf` de la carpeta de
  modelos y no está cargado, se carga en un slot nuevo antes de reenviar la petición; si falta memoria se expulsan
  por LRU los slots no fijados (`pinned: false`) sin peticiones en curso. Los fijados nunca se expulsan.
- **`EngineDriver`** (`dllm/engine.py`): el slot ya no asume `llama-server`. `LlamaCppDriver` implementa
  `build_command`, `healthcheck`, `estimate_vram`, `supported_endpoints` y `default_placement`; añadir un motor es
  registrar otro driver. Nuevos ajustes de modelo: `embeddings` (lanza con `--embeddings`), `placement`
  (`auto`/`split`/`node`: un modelo de embeddings va por defecto fijado a un único nodo) y `engine`.
- `dllm load --slot N|new --on-demand`.
- **Panel**: en *Modelos*, selector de destino (modelo principal o slot nuevo) y casilla *Fijar*; tabla de modelos
  cargados (estado, uso libre/ocupado, política, nodos, puerto) con memoria reservada; y los ajustes de dispositivo,
  reparto y modo embeddings en los parámetros de cada modelo.
- **Libro de memoria VRAM + RAM**: dos dimensiones por nodo. En nodos con GPU el aporte es VRAM y se reserva además un
  buffer de E/S en RAM; en nodos solo-CPU el aporte es RAM. El equipo del orquestador figura como `@host`. Márgenes de
  seguridad configurables: `--vram-safety-mb` (256) y `--ram-safety-mb` (1024); la RAM libre real (`MemAvailable`,
  `vm_stat`, `GlobalMemoryStatusEx`) actúa como tope.
- **`device_policy` por modelo**: `auto` (como hasta ahora), `gpu_strict` (solo nodos con GPU; si no cabe, error
  limpio), `cpu_only` (`-ngl 0`: el modelo corre entero en la RAM del orquestador y no usa VRAM, pensado para
  embeddings u otros auxiliares fijados) y `gpu_with_cpu_fallback` (todo en GPU si cabe; si no, las capas que quepan
  con `-ngl N` y el resto en la RAM del orquestador; si ni una cabe, `-ngl 0`).
- **Estados IDLE/BUSY y cola con contrapresión**: cada slot cuenta sus peticiones en curso (`active_requests`) y solo se
  expulsan por LRU los no fijados y sin peticiones. Si la memoria está ocupada por modelos en uso, la petición espera en
  una cola acotada (`--queue-timeout`, 30 s) y, vencido el plazo, recibe `429` con `Retry-After`. Si el modelo no cabe
  ni descontando todo lo no fijado, `503` **antes** de lanzar ningún proceso y sin expulsar nada. Los errores usan el
  formato de OpenAI.
- Los `llama-server` de los slots escuchan en `127.0.0.1:18081…` (el primer puerto libre del rango).
- `dllm up --allow-ip IP`: el orquestador solo acepta peticiones de esas IP (además de loopback).
- `EngineDriver.estimate_memory()` devuelve la VRAM y la RAM que necesita según la política.

### Cambiado
- Antes se llenaba por ancho de banda de memoria; ahora un nodo lento pero cercano puede preferirse a uno rápido
  pero lejano, y un único nodo grande a varios pequeños.
- Los nodos solo-CPU ahora reservan en la dimensión RAM del libro (el reparto es el mismo).
- Con un solo modelo el comportamiento no cambia (slot 0, sin `--on-demand`).
- `min_mbps` (800 por defecto) sigue siendo un mínimo duro: un Wi-Fi lento queda excluido, no es tier 3.

## [0.4.0] - 2026-11-15

### Añadido
- **Unir nodos sin código**: un nodo instalado sin código de unión aparece en el panel del orquestador (*Nodos →
  Equipos esperando unirse*). Al reclamarlo, el panel y el nodo muestran el mismo código de verificación de 6 cifras;
  al aceptarlo en el nodo (asistente o `dllm pair`), recibe el secreto cifrado (Diffie-Hellman + HMAC-SHA256) y se une
  sin reiniciar. Con código de unión todo sigue igual.
- **Backends para todos los sistemas**: CUDA precompilado también en Linux (x64 y ARM64), ROCm y SYCL en Windows y
  Linux, Vulkan ARM64. Se elige la versión de CUDA más alta que admite el driver y, con *auto*, si un backend falla se
  prueba el siguiente (CUDA → Vulkan → CPU). El asistente explica cada opción.
- **`dllm open` y acceso directo «DLLM»** (escritorio y menú Inicio en Windows; menú de aplicaciones en Linux):
  arranca DLLM si está parado, abre el panel y esconde la consola; si se cierra el navegador con DLLM en marcha, la
  consola reaparece para reabrirlo o pararlo. El asistente esconde también su consola mientras está abierto.
  `Open-DLLM.bat` en el paquete.
- **Fecha y hora del servidor para el modelo** (parámetro por modelo, activado por defecto: fecha y hora, solo fecha
  o nada), también cuando el cliente trae su propio prompt de sistema.
- Enlace «para otros equipos» (`…/#key=…`) en el asistente y en el botón *API key* del panel: abre el panel desde otro
  equipo sin escribir la clave.

### Cambiado
- El panel **no pide API key desde el propio equipo** (127.0.0.1). Desde la red sigue siendo obligatoria si se expone.
- Las peticiones `POST` al panel con `Origin` de otro sitio se rechazan (anti CSRF / DNS rebinding).

### Corregido
- Latencia medida entre equipos inflada (~40 ms en la primera prueba real): las respuestas HTTP salían en dos envíos y
  el segundo esperaba al ACK retardado del otro equipo. Ahora con `TCP_NODELAY`. Afectaba a los «ms» del panel y a la
  estimación de tokens/s, no al túnel de inferencia (que ya lo usaba). Pendiente de confirmar en la red real.

## [0.3.7] - 2026-11-14

Primera prueba real: Gemma 4 31B repartido entre una RX 7600 XT y una RTX 3060 (Windows, Vulkan).

### Corregido
- **Contexto máximo**: la caché KV se calculaba como si todas las capas guardaran todo el contexto. Ahora se tienen
  en cuenta la atención de ventana deslizante (patrón por capa del GGUF, o el conocido de Gemma 2/3 y Cohere 2), las
  cabezas KV y dimensiones por capa y las capas con caché compartida. Con Gemma 4 31B y 22 GiB: de ~2.800 a ~32.000
  tokens (f16) o ~74.000 (q8_0). Validado con la configuración publicada del modelo, no con su GGUF.
- Windows: ya no se abren ventanas de CMD al lanzar `rpc-server`, `llama-server`, `nvidia-smi`, etc.
- Windows: el monitor no mostraba uso ni VRAM de GPUs AMD/Intel (el código de retorno de PDH se leía con signo).
- Columna *llama.cpp* con «?»: `llama-server --version` puede tardar más de 15 s en Windows al cargar los backends;
  ahora se espera hasta 60 s y, si no responde, la versión se toma del nombre de la carpeta. Las versiones se comparan
  por número (`b12387`).

## [0.3.6] - 2026-11-14

### Añadido
- **Carpeta de DLLM** elegible en el asistente (por defecto `~/DLLM`, para todos los roles): ahí se instalan
  llama.cpp (`llama.cpp/`) y los modelos (`models/`), también las actualizaciones de llama.cpp que pida el
  orquestador. La configuración y el secreto siguen en `~/.dllm`. Al reinstalar sobre una versión anterior se propone
  la carpeta de modelos que ya había y los modelos se quedan donde están. Opciones `--install-dir` y `--models-dir`.
- **Cortafuegos automático en Windows**: reglas `DLLM-agente`, `DLLM-beacon` y `DLLM-API` limitadas al ejecutable
  de Python, a los puertos necesarios y a la IP del orquestador o la subred local, con una sola confirmación de
  administrador. Se borran al desinstalar. `--no-firewall` para hacerlo a mano. Linux/macOS: se muestran los comandos.
- **Desinstalador** para agente, orquestador o ambos: `Uninstall-DLLM.bat`, `Uninstall-DLLM.command` y
  `uninstall.sh` en el paquete, botón *Desinstalar* en el asistente (opción 4 en terminal) y
  `dllm uninstall [--purge] [--models] [--interactive]`. Los modelos solo se borran si se pide y si están en la
  carpeta de DLLM.

### Cambiado
- En el rol *Ambos* el agente escucha solo en `127.0.0.1` (antes en todas las interfaces aunque solo aceptara al
  propio equipo): no necesita reglas de cortafuegos ni provoca el aviso de Defender.

## [0.3.5] - 2026-11-14

### Corregido
- Instalación: «rpc-server no arranca: … unknown argument: --version». El servidor RPC de llama.cpp no admite
  `--version`; ahora se comprueba con `--help` (que también carga los backends) y su build se lee del
  `llama-server` que viene en el mismo paquete. Probado solo con binarios simulados que imitan el comportamiento real.

## [0.3.4] - 2026-11-14

### Corregido
- Windows: si el **Control inteligente de aplicaciones** (Smart App Control) bloquea las DLL sin firmar de llama.cpp,
  la instalación ya no se queda colgada en un diálogo «no está diseñado para ejecutarse en Windows» con el error
  «timed out»: el fallo se detecta (código `0xc0e90002`) y se explica cómo desactivarlo. Lo mismo para
  `rpc-server` en el agente, y para otros bloqueos de carga (DLL que falta, arquitectura incorrecta).
- `dllm doctor` avisa si el Control inteligente de aplicaciones está activo o en evaluación.

## [0.3.3] - 2026-11-14

### Corregido
- Instalación: «el paquete descargado no contiene llama-server y rpc-server». llama.cpp renombró el servidor RPC a
  `ggml-rpc-server`; ahora se aceptan los dos nombres (instalador, agente, asistente, `dllm doctor`) y
  `build-llamacpp.sh` compila el target nuevo (con el antiguo como alternativa).
- Windows: el selector de carpetas del asistente se abría detrás del navegador. Ahora se abre delante y con el foco.
  La ruta inicial se pasa al diálogo por variable de entorno, no dentro del script de PowerShell.

## [0.3.2] - 2026-11-14

### Corregido
- Instalación: «no se encontraron compilaciones de llama.cpp». Desde `v1.0.0`, llama.cpp publica sus compilaciones
  `bNNNN` como *pre-release* y la 0.3.1 las descartaba. Ahora se aceptan (los borradores se siguen ignorando).

## [0.3.1] - 2026-11-14

### Corregido
- **Instalación de llama.cpp:** ya no se usa la release «Latest» de llama.cpp, que ahora es `v1.0.0` y no trae
  binarios. Se elige la compilación `bNNNN` más reciente que publica todos los paquetes habituales (Windows
  CPU/Vulkan/CUDA, Linux, macOS), para que cualquier nodo encuentre el suyo en la misma versión que el orquestador.
  Antes, un nodo Windows con Vulkan podía fallar con «esta release no incluye el backend Vulkan para Windows».
- Si un nodo recibe una versión sin su paquete, el error explica qué hacer (actualizar el orquestador y usar su nuevo
  código de unión).

### Añadido
- **Selector de carpeta de modelos** en el asistente: diálogo nativo del sistema (Windows, macOS, zenity/kdialog o Tk
  en Linux) o, sin escritorio, un explorador de carpetas en la propia página (navegar, unidades de Windows, crear
  carpeta). Muestra el espacio libre del disco. La carpeta se crea al instalar si no existe.

## [0.3.0] - 2026-11-14

### Añadido
- Licencia **GNU AGPL-3.0-or-later** con términos adicionales de atribución al autor original (`NOTICE`,
  sección 7.b y 7.c), `AUTHORS.md` y `CONTRIBUTING.md` (contribuciones con DCO, sin CLA).
- Avisos legales y enlace al código fuente en el panel web, el asistente, `dllm --version` y el registro del
  orquestador (AGPL 5.d y 13). Cabeceras SPDX en todos los ficheros de código.
- El paquete distribuible incluye `LICENSE`, `NOTICE` y `AUTHORS.md`.
- **Gestión de lo que aporta cada nodo** (memoria e hilos): modo *automático* (lo ajusta el orquestador desde el panel,
  con valor recomendado) o *manual* (fijado en el propio nodo, solo lectura en el panel). Porcentaje del equipo o
  valores exactos; los hilos se aplican a `rpc-server -t`. Paso nuevo en el asistente, opciones
  `--share/--threads/--limits-mode`, botón **Ajustar** en el panel y `POST /api/node/limits`. Los cambios se guardan
  en el nodo.
- **Contexto máximo**: el reparto calcula el mayor contexto que cabe con la memoria de los nodos (con tope en el de
  entrenamiento del modelo), lo muestra con un botón *Usar* y avisa si se pide más del que admite el modelo.

- **Gestión de modelos en el panel**: búsqueda y descarga desde **Hugging Face** (incluidos troceados y modelos con
  acceso restringido mediante token), descarga desde el registro de **Ollama** (con sus parámetros por defecto) e
  importación desde una **instalación local de Ollama**. Cola con progreso, cancelación, reanudación, comprobación de
  espacio y verificación sha256 + GGUF. Eliminar modelos.
- **Parámetros por modelo** (temperatura, top-p/k, min-p, penalización de repetición, máx. tokens, paradas, prompt de
  sistema, contexto al cargar), aplicados a todas las peticiones sin pisar los valores del cliente.
- **Modelos multimodales (visión/audio)**: el proyector `mmproj` se descarga junto al modelo (HF y Ollama), se detecta
  en la carpeta del modelo y se activa al cargar (`--mmproj`, en CPU del orquestador). El chat del panel permite
  adjuntar imagen o audio.

- **Agente autoconfigurable**: detecta sus dispositivos preguntando a llama.cpp (NVIDIA, AMD, Intel, Apple),
  presta la GPU con más memoria (`rpc-server -d`, elegible con `--device`) e informa de su VRAM real. El asistente
  muestra la GPU detectada y el panel el dispositivo de cada nodo.
- **Sincronización de llama.cpp**: el orquestador pide a los nodos con otra build que instalen la suya
  (`POST /llama/install`, paquete oficial con sha256). Desactivable con `--no-auto-update` / `--no-auto-sync`.
- **El orquestador usa solo los dispositivos remotos** (`--device` a partir de `--list-devices`): su propia GPU no
  entra en el reparto, así que el rol *Ambos* funciona en un PC con GPU.

- **Monitor en tiempo real** (pestaña nueva del panel): por nodo, uso de GPU, VRAM, consumo y límite, temperatura,
  ventilador, reloj, CPU, RAM y tráfico RPC con gráficas; del clúster, tokens/s (leídos de `/metrics` de
  llama-server), peticiones en curso y VRAM total. Fuentes sin dependencias: `nvidia-smi`, sysfs de amdgpu,
  contadores WDDM de Windows (PDH, independiente del idioma) e `ioreg`. API `GET /api/telemetry`.
- **Ajustes por nodo en el orquestador**: activar/desactivar, capas fijas y ancho de banda (`POST /api/node/settings`);
  dispositivo a prestar elegible desde el panel.
- **Parámetros de carga por modelo**: tipo de caché K/V (`q8_0`, `q4_0`…), flash attention, lotes, peticiones
  simultáneas y RoPE, aplicados a `llama-server`; el reparto calcula la memoria con el tipo de caché elegido.
  Nuevos parámetros de generación: typical-p, penalizaciones de presencia y frecuencia, semilla.
- **Panel rediseñado en pestañas** (Monitor, Nodos, Modelos, Chat, Eventos); chat con historial de conversación.

### Cambiado
- Al caer un nodo, el estado pasa a `RECOVERING` en el mismo instante de la detección (antes podía verse `READY`
  con el reparto antiguo durante un momento).
- En nodos con GPU, el valor recomendado de memoria es la VRAM libre menos la reserva (antes, 75 % del total).
- `--mem-gb` de un nodo ya no puede superar su memoria total menos la reserva (se recorta y se indica).
- El panel escapa los nombres de nodo y los motivos de exclusión al mostrarlos.

## [0.2.1] - 2026-11-11

Primera versión publicada en GitHub.

### Añadido
- Documentación completa en `docs/`: instalación, uso, arquitectura, seguridad, solución de problemas y desarrollo.
- CI en GitHub Actions (Linux, macOS, Windows · Python 3.9 y 3.13) y publicación automática de releases al etiquetar.
- Opción *Dirección del orquestador para los nodos* (`--advertise-host`) en el asistente y en `dllm join-code`, para
  clústeres sobre WireGuard/VPN o con varias interfaces.

### Corregido
- Lanzador de Windows: la respuesta «n» a «¿Instalar Python?» se ignoraba.
- Rol *Ambos*: el agente local se anunciaba por broadcast y aparecía duplicado como nodo caído.
- Agente, orquestador y asistente tardaban hasta ~35 s en empezar a responder en macOS (y de forma intermitente en
  Linux) por la resolución DNS inversa que hace el servidor HTTP de Python al arrancar.
- Tests portables a Windows y macOS.

## [0.2.0] - 2026-11-04

### Añadido
- Paquete único para todos los equipos (`dllm.pyz` + lanzadores para Windows, macOS y Linux).
- Asistente de instalación en navegador y en terminal: orquestador, nodo o ambos.
- Código de unión `DLLM1-…` (secreto, dirección del orquestador y build de llama.cpp).
- Descarga de llama.cpp oficial por plataforma/backend con verificación sha256 y extracción segura.
- Arranque automático de usuario: systemd, launchd, tarea programada.
- Comandos `setup`, `run`, `join-code`, `doctor`, `uninstall`.

## [0.1.0] - 2026-11-04

### Añadido
- Agente de nodo con `rpc-server` solo en loopback y proxy con handshake HMAC.
- Orquestador: descubrimiento por beacons firmados, benchmark de red, scheduler, `llama-server` sobre túneles,
  API OpenAI-compatible, recuperación ante caídas, métricas Prometheus y panel web.
