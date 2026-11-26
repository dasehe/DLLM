# Solución de problemas

Primero, en el equipo afectado:

```bash
dllm doctor        # Python, configuración, binarios de llama.cpp, misma build, programa en marcha
dllm status        # (en el orquestador) nodos, estado, plan y por qué se excluye cada nodo
```

Registros: Linux `journalctl --user -u dllm -f` · macOS `~/.dllm/dllm.log` · Windows: ejecuta `dllm run` en una
consola para verlo en directo (la tarea programada corre sin ventana). En el panel, **Registros** muestra las últimas
líneas de `llama-server` cuando una carga falla.

## El modelo se reinicia (RECOVERING) al enviar una imagen

Mira *Registros*: tras «llama-server terminó (código N)» aparecen las líneas del fallo. Si ves
`GGML_ASSERT … non-causal attention requires n_ubatch >= n_tokens`, el lote físico es menor que los tokens de la imagen
(Gemma 3/4 procesan la imagen con atención no causal en un único lote; Gemma 4 admite hasta 1120 tokens por imagen).
Solución: en *Modelos → Parámetros* sube **Lote lógico (batch)** y **Lote físico (ubatch)** (p. ej. 2048; el físico
nunca puede superar al lógico). Un lote mayor gasta más VRAM de cómputo: baja el contexto si hace falta. No reenvíes la
imagen hasta recargar el modelo con el cambio.

Si en cambio aparece `ggml-rpc.cpp … Remote RPC server crashed or returned malformed response` o `recv returned 0
(peer closed?)`, el que ha muerto es el **`rpc-server` del nodo** (por eso `llama-server` sale con un código como
`3221226505`, un aborto): no es el lote. Desde 0.6.0 *Registros* añade debajo «rpc-server de <nodo> terminó (código N)»
con sus últimas líneas (fallo del driver Vulkan/CUDA, memoria agotada de la GPU…), y `/api/state` las incluye en
`last_crash.rpc`; el agente las guarda también en su log y en `rpc_last_exit` de `/info`. Con eso se distingue un
fallo del driver de la GPU de una falta de memoria de cómputo (en ese caso prueba un contexto menor).

## Instalación

| Síntoma | Causa probable | Solución |
|---|---|---|
| «DLLM necesita Python 3.9…» | Python ausente o antiguo | Acepta instalarlo o descárgalo de python.org (en Windows marca *Add to PATH*) |
| macOS: «no se puede abrir porque es de un desarrollador no identificado» | Gatekeeper | Clic derecho → Abrir, o `xattr -dr com.apple.quarantine CARPETA` |
| «no se pudo consultar las releases de llama.cpp» | Sin Internet, proxy o límite de la API de GitHub (60/h por IP) | Reintenta más tarde o usa `--bin-dir` con llama.cpp descargado a mano |
| «… La versión bNNNN la fija el orquestador … no publicó ese paquete» | El orquestador se instaló con una versión de llama.cpp incompleta (DLLM ≤ 0.3.0) | Actualiza DLLM en el orquestador, vuelve a ejecutar su asistente y usa el nuevo código de unión en los nodos (los ya instalados se actualizan solos) |
| «no hay paquete de llama.cpp para …» | Combinación sistema/backend sin binario oficial (p. ej. CUDA en Linux) | Otro backend o compilar con `build-llamacpp.sh` + `--bin-dir` |
| «el paquete descargado no contiene llama-server y rpc-server» | DLLM < 0.3.3: llama.cpp renombró `rpc-server` a `ggml-rpc-server` | Actualiza DLLM a 0.3.3 o posterior. Si persiste, esa build no incluye RPC: compila con `build-llamacpp.sh` (activa `GGML_RPC=ON`) |
| El selector de carpetas no aparece (está detrás de otras ventanas) | DLLM < 0.3.3 en Windows | Actualiza DLLM; o minimiza el navegador para verlo |
| Una GPU NVIDIA (p. ej. en una VM de Proxmox con *passthrough*) se instala con Vulkan en vez de CUDA | El instalador solo elige CUDA si `nvidia-smi` funciona y su driver admite CUDA ≥ 12.4; la virtualización no lo impide si el driver del invitado es reciente. El backend se fija al instalar y el panel no lo cambia | En el nodo: `nvidia-smi` (debe mostrar «CUDA Version: 12.4» o más); actualiza el driver y repite el asistente con `--backend cuda` (`py -3 dllm.pyz setup --backend cuda`). El asistente avisa del motivo |
| Al reclamar un nodo: «No se pudo reclamar: este nodo ya está emparejado» | Otro proceso (un agente de una instalación anterior que no se cerró al desinstalar) tiene el puerto del agente y contesta en lugar del nuevo | En el nodo: Administrador de tareas → cerrar `pythonw.exe`/`python.exe` antiguos (o reiniciar el equipo) y volver a iniciar DLLM; comprueba con `netstat -ano \| findstr :<puerto>`. Desde 0.6.0 el agente se niega a arrancar si el puerto está ocupado y lo explica |
| Windows: «`[WinError 4551]` Una directiva de Control de aplicaciones bloqueó este archivo» al instalar (antes el asistente seguía con CPU, que también se bloquea), «llama-server-impl.dll no está diseñado para ejecutarse en Windows… 0xc0e90002», aviso de Defender «Se ha bloqueado parte de esta aplicación», o «llama-server no arranca … timed out» | El **Control inteligente de aplicaciones** (Smart App Control) de Windows 11 bloquea las DLL sin firmar; los binarios oficiales de llama.cpp no van firmados | Seguridad de Windows → Control de aplicaciones y navegador → Configuración de Control inteligente de aplicaciones → **Desactivado**, y repite la instalación. Desde la actualización de abril de 2026 se puede volver a activar sin reinstalar Windows. No hay excepciones por programa. `dllm doctor` avisa si está activo |
| «rpc-server no arranca: … error: unknown argument: --version» | DLLM ≤ 0.3.4: el servidor RPC de llama.cpp no tiene `--version` | Actualiza DLLM a 0.3.5 o posterior y repite la instalación (no hace falta borrar lo descargado) |
| Aviso de Defender «Firewall de Windows ha bloqueado algunas características de Python» | DLLM ≤ 0.3.5 no creaba reglas del cortafuegos | Desde 0.3.6 el asistente crea reglas `DLLM-*` limitadas. Si ya aceptaste el aviso, puedes borrar las reglas amplias de `python.exe`/`pythonw.exe` (ver [Cortafuegos](INSTALACION.md#cortafuegos)) |
| Windows: se abren ventanas de CMD al cargar un modelo | DLLM ≤ 0.3.6 | Actualiza a 0.3.7 (los programas de llama.cpp se lanzan sin ventana) |
| GPU AMD/Intel en Windows sin uso ni VRAM en el monitor | DLLM ≤ 0.3.6 no leía bien los contadores de Windows | Actualiza a 0.3.7. Temperatura, consumo y ventilador siguen sin estar disponibles en Windows para AMD/Intel |
| Contexto máximo muy bajo con Gemma (p. ej. 2816 tokens) | DLLM ≤ 0.3.6 calculaba la caché KV como si todas las capas guardaran todo el contexto | Actualiza a 0.3.7; con caché `q8_0` + flash attention cabe aún más |
| «el sha256 … no coincide» | Descarga corrupta o manipulada | Reintenta; si persiste, revisa proxy/antivirus. **No** lo ignores |
| «rpc-server no arranca … libvulkan» | Falta el runtime Vulkan | `sudo apt install libvulkan1 mesa-vulkan-drivers` o driver del fabricante, o backend `cpu` |
| «código incompleto o con errores» | El código se cortó al copiar | Cópialo entero de nuevo (`dllm join-code`); espacios y saltos de línea se toleran |
| Aviso de *linger* en Linux | El servicio solo corre con la sesión abierta | `sudo loginctl enable-linger $USER` |
| Arranque automático no instalado | Sin systemd de usuario (contenedor, WSL…) | Arranca con `dllm run` (p. ej. en `tmux` o un servicio del sistema) |

## Los nodos no aparecen en el panel

1. ¿Está el agente en marcha en el nodo? `dllm doctor` allí.
2. ¿Llega el descubrimiento? El nodo envía UDP 50999 al orquestador: abre ese puerto en el **orquestador**.
3. ¿Distintas subredes o VPN? Reinstala el orquestador con `--advertise-host IP-alcanzable` y los nodos con el nuevo
   código, o añádelos a mano arrancando el orquestador con `dllm up --node IP-NODO …` (modo manual).
4. Aparece **rojo** con «HTTP 403»/«firma inválida»: el nodo tiene otro secreto o la IP del orquestador cambió
   (el nodo solo acepta la IP que venía en el código). Reinstala el nodo con el código actual.
5. «firma inválida» intermitente: relojes desincronizados más de 60 s. Activa NTP en todos los equipos.
6. Nodo instalado **sin código** que no sale en *Equipos esperando unirse*: los anuncios van por broadcast al puerto
   UDP 50999 y no cruzan subredes ni VPN; usa el código de unión. Si al reclamarlo «caduca», confirma en el nodo en
   menos de 5 min (página final del asistente o `dllm pair`). Si el código no coincide, **rechaza**: otro equipo de la
   red ha intentado reclamarlo. Alternativas sin broadcast: en el panel, **Nodos → Añadir por IP** o **Buscar en la red**; al
   instalar el nodo, indica la IP del orquestador (`--orch-ip`).

## Un nodo tiene GPU pero Monitor dice «sin GPU» (aviso «llama.cpp no la usa»)

`nvidia-smi` ve la GPU, pero el `rpc-server` instalado es solo CPU (o al arrancarlo faltaba el driver/Vulkan), así que no puede
prestarla: el nodo se anuncia como CPU/RAM y muestra el aviso. En el nodo, vuelve a ejecutar el asistente eligiendo
CUDA o Vulkan, o instala el driver; el agente vuelve a detectar los dispositivos cada minuto, sin reiniciar. Comprueba con
`rpc-server --list-devices` y `python3 ~/.dllm/dllm.pyz doctor`.

## Un nodo sale excluido del reparto

`dllm plan MODELO` dice el motivo:

| Motivo | Solución |
|---|---|
| `red X Mbps < mínimo 800 Mbps` | Cable/switch Gigabit; revisa negociación (`ethtool eth0`, debe decir 1000Mb/s). Último recurso: `--min-mbps`, o *Nodos → Usar igualmente* para ese nodo (modo forzado, pide confirmación). Si cambias el cable, repite *Medir la red* |
| `build distinta (…); se le pedirá actualizar` | Normal: el nodo instalará solo la build del orquestador en unos minutos («actualizando llama.cpp … %») |
| `build distinta y la actualización falló: …` | Sin Internet en el nodo, o no hay paquete para su sistema/backend: instala esa build a mano con `--bin-dir`, o reinstala el nodo con el código actual |
| `build distinta (…): reinstala el nodo…` | El nodo tiene la actualización automática desactivada: reinstálalo con el código de unión actual |
| `rpc-server no instalado` | Reinstala el nodo |
| `memoria libre insuficiente` | El nodo aporta muy poco: súbelo con **Configurar** en el panel (nodo automático) o en el propio nodo (manual) |
| `sin respuesta` | Ver sección anterior |

## El nodo no presta la GPU que esperaba

- El panel muestra en *Dispositivo* lo que ha detectado. Si sale «CPU» en un equipo con GPU: el paquete de llama.cpp
  instalado no tiene backend de GPU (reinstala el nodo eligiendo `cuda`/`vulkan`) o faltan los drivers (Vulkan en
  AMD/Intel, driver NVIDIA reciente para CUDA).
- Con varias GPUs se usa la de más memoria; para otra, `--device` (p. ej. `CUDA1` o `7600`).
- Comprobación manual en el nodo: `ggml-rpc-server -d x` (`rpc-server` en builds antiguas) lista los dispositivos que ve llama.cpp.

## El monitor muestra «—»

- Es un dato que ese sistema no ofrece sin software adicional: en Windows, la temperatura y el consumo de GPUs AMD o
  Intel; en macOS, consumo y temperatura. Uso y VRAM sí deberían verse.
- NVIDIA sin datos: comprueba que `nvidia-smi` funciona en el nodo (viene con el driver).
- AMD en Linux sin consumo/temperatura: kernel o driver antiguo; comprueba que existe
  `/sys/class/drm/card*/device/hwmon`.
- Ningún dato del nodo: el agente es de una versión anterior; reinstálalo con el asistente.

## La carga falla

| Mensaje | Solución |
|---|---|
| `no cabe: necesito X GiB … aportan Y GiB` | Más nodos, que los nodos aporten más (**Configurar**), un modelo más cuantizado o menos `--ctx` (el mensaje indica el contexto máximo que cabe) |
| El panel dice «el nodo fija sus límites localmente» | Ese nodo está en modo manual: cámbialo en el propio nodo con el asistente |
| «recortado al máximo» junto a la memoria de un nodo | Se pidió más de lo que tiene el equipo (total − reserva); se usa el máximo posible |
| `llama-server terminó: … failed to allocate` | La estimación se quedó corta: baja `--ctx` o la memoria ofrecida en algún nodo |
| `llama-server no estuvo listo en 1800 s` | Modelo enorme por red lenta; amplía `--load-timeout` |
| `las capas fijadas (X) superan las del modelo` · `N capas necesitan X GiB y aporta Y` | Revisa **Capas fijas** en *Nodos → Configurar* (vacío = automático) |
| `la caché V cuantizada requiere flash attention` | En los parámetros del modelo, pon flash attention en `auto` u `on`, o la caché V en `f16` |
| `los nodos exponen N dispositivos y se esperaban M` | Algún nodo expone varias GPUs (agente antiguo o `--rpc-arg -d …`): actualiza el agente o fija `--device` |
| `aviso: … se le asignan X GiB y su dispositivo tiene Y GiB libres` | Otro programa usa esa GPU: ciérralo o baja lo que aporta el nodo con **Configurar** |
| `túnel → … falló` en eventos | El agente no acepta la conexión: secreto, `allow_ip` o cortafuegos del puerto 50051 |
| Reparto absurdo (todo a un nodo lento) | Las estimaciones de ancho de banda no reflejan tu hardware: fija `--bw-gbps` por nodo |

## Descargas de modelos

| Mensaje | Solución |
|---|---|
| `HTTP 401/403 … acceso restringido` | Acepta las condiciones del modelo en su página de Hugging Face y guarda tu token en el panel |
| `no se pudo conectar con huggingface.co / registry.ollama.ai` | El orquestador no tiene salida a Internet o hay un proxy: descarga el `.gguf` en otro equipo y cópialo a la carpeta de modelos |
| `no hay espacio: hacen falta X GiB` | Libera espacio o cambia la carpeta de modelos (asistente del orquestador) |
| `el sha256 no coincide` · `no es un fichero GGUF` | Descarga dañada o manipulada: se descarta sola; vuelve a intentarlo |
| `descarga incompleta` | Se cortó la conexión: pulsa descargar otra vez y se reanuda |
| Un modelo de Ollama no carga (`llama-server terminó: … unknown model architecture` o similar) | Ese modelo usa el formato propio de Ollama: descarga la versión GGUF de Hugging Face |
| No aparece **📎 Adjuntar** en el chat | El modelo no tiene proyector: descárgalo de su repo (`mmproj-*.gguf`) a la misma carpeta, o elige uno en *Parámetros*, y recarga el modelo |

## Va muy lento

- Mira los **Mbps y ms** de cada nodo en el panel: > 1 ms de RTT ya penaliza. Hasta la 0.3.7 la medida entre equipos
  podía salir unos **40 ms** más alta de lo real por un artefacto de TCP (corregido en la 0.4.0); compárala con `ping`.
- Usa menos nodos: si el modelo cabe en dos, el tercero solo resta.
- Un nodo con poca CPU/memoria lenta limita a todos: retíralo (para su agente) y recarga.
- Compara con el mismo modelo en un solo equipo para saber cuánto cuesta el reparto.

## Reiniciar de cero

```bash
dllm uninstall --purge     # en cada equipo
```
y vuelve a instalar empezando por el orquestador.
