# Instalación

El **mismo paquete** sirve para todos los equipos; el asistente pregunta qué papel tendrá cada uno.

| Rol | Qué hace | Cuántos |
|---|---|---|
| **Orquestador** | Guarda los modelos, reparte la carga, ofrece el panel y la API. No aporta cómputo. | Uno por clúster |
| **Nodo** | Presta su CPU/GPU y su memoria al orquestador. | Los que quieras |
| **Ambos** | Orquestador que además aporta su propia CPU/GPU como un nodo más. | Uno por clúster |

## 1. Requisitos

| | Mínimo | Recomendado |
|---|---|---|
| Sistema | Windows 10/11, macOS 12+, Linux (Ubuntu 22.04+, Debian 12+, Fedora…) | — |
| Python | 3.9 | 3.11+ |
| Red | 1 Gbps entre orquestador y nodos (cable) | 2,5/10 Gbps, misma subred |
| Memoria | La suma de RAM/VRAM libre de los nodos ≥ tamaño del modelo + ~10 % | Margen de 2 GiB por nodo |

El scheduler excluye los nodos con menos de **800 Mbps** medidos (Wi-Fi suele quedar fuera). Se puede cambiar con
`--min-mbps`, a costa de mucha más lentitud.

## 2. Descargar el paquete

Descarga `dllm-package-<versión>.zip` de [Releases](https://github.com/dasehe/dllm/releases), compruébalo si quieres
contra `SHA256SUMS` y descomprímelo. Contenido:

```
dllm.pyz               el programa completo (un solo archivo)
Install-DLLM.bat       asistente para Windows (doble clic)
Install-DLLM.command   asistente para macOS (doble clic)
install.sh             asistente para Linux (y macOS por terminal)
Open-DLLM.bat          abrir el panel en Windows (lo mismo que el acceso directo DLLM)
Uninstall-DLLM.bat     desinstalador para Windows (doble clic)
Uninstall-DLLM.command desinstalador para macOS (doble clic)
uninstall.sh           desinstalador para Linux (y macOS por terminal)
build-llamacpp.sh      compilar llama.cpp desde fuente (CUDA en Linux, etc.)
LEEME-INSTALAR.md      resumen de estas instrucciones
SHA256SUMS             sumas de comprobación
```

> El repositorio es privado: para descargar la release hay que iniciar sesión en GitHub con una cuenta con acceso.
> También puedes generar el paquete tú mismo: `python scripts/build_package.py` (ver [DESARROLLO](DESARROLLO.md)).

## 3. Instalar el orquestador

Abre el asistente (tabla del README) y:

1. Elige **Orquestador** o **Ambos**.
2. Indica la **carpeta de DLLM** con **Examinar…** (por defecto `~/DLLM`): ahí se instalan llama.cpp
   (`llama.cpp/`) y los modelos (`models/`). Se abre el selector de carpetas de tu sistema, delante del navegador
   (o, si no hay escritorio, un explorador dentro del asistente que también permite crear carpetas). Debajo verás el
   espacio libre del disco: un modelo de 7B ocupa ~5 GB y uno de 32B ~20 GB. La carpeta se crea si no existe. La
   configuración y el secreto del clúster se quedan aparte, en `~/.dllm`, con permisos restringidos.
3. Marca *Permitir usar la API desde otros equipos* solo si la vas a usar desde fuera del propio orquestador: se
   generará una **API key** obligatoria.
4. Pulsa **Instalar**. El asistente:
   - descarga la versión oficial más reciente de llama.cpp que publica paquetes para todos los sistemas (así
     cualquier nodo encontrará el suyo de la misma versión) y verifica su **sha256**;
   - guarda la configuración en `~/.dllm/config.json` (permisos 600, contiene el secreto del clúster);
   - copia el programa a `~/.dllm/dllm.pyz`;
   - registra el **arranque automático** de usuario (no necesita administrador);
   - en Windows, abre en el cortafuegos **solo** los puertos necesarios y limitados a tu red (pide permiso de
     administrador una vez; ver [Cortafuegos](#cortafuegos));
   - comprueba que el orquestador responde.
5. Copia el **código de unión** `DLLM1-…` que aparece al final. Puedes volver a verlo con `dllm join-code`.

> El código de unión contiene el **secreto del clúster**: trátalo como una contraseña y no lo pegues en chats públicos.

## 4. Instalar cada nodo

1. Abre el asistente y elige **Nodo**.
2. Pega el **código de unión**, o déjalo vacío para **reclamarlo desde el orquestador** (ver abajo).
3. Elige **cuánto comparte este equipo**:
   - **Automático** (recomendado): lo gestiona el orquestador y se puede cambiar desde su panel web.
   - **Limitar desde este equipo**: porcentaje (p. ej. 33 %) y, si quieres, memoria y hilos exactos. El orquestador
     no podrá cambiarlo.

   El asistente muestra lo que supone cada opción en tu equipo. Más detalle en
   [USO](USO.md#gestionar-lo-que-aporta-cada-nodo).
4. (Opcional) *Opciones avanzadas*: nombre del equipo, backend.
5. **Instalar**. El nodo descarga **exactamente la misma build** de llama.cpp que el orquestador (va en el código),
   y queda aceptando conexiones **solo** de la IP del orquestador.

En unos segundos aparecerá en el panel del orquestador (indicador verde).

### Unir un nodo sin código (reclamarlo desde el orquestador)

Si instalas el nodo sin código de unión, queda **esperando** y se anuncia en la red local (solo su nombre y puerto):

1. En el panel del orquestador: **Nodos → Equipos esperando unirse → Reclamar**. Aparece un **código de verificación**
   de 6 cifras.
2. En el nodo (la página final del asistente, o `dllm pair` / el acceso *Open-DLLM* en ese equipo) aparece la
   solicitud con el **mismo código**. Si coincide, pulsa **Aceptar**.
3. El orquestador le envía el secreto del clúster cifrado y el nodo se une sin reiniciar. Desde ese momento solo
   acepta conexiones de ese orquestador.

El código evita que otro equipo de la red se haga pasar por el orquestador; ver [SEGURIDAD](SEGURIDAD.md#emparejamiento-sin-código).
La solicitud caduca a los 5 minutos. Con código de unión no hace falta nada de esto.

## 5. Primer modelo

1. Añade un modelo desde el panel (*Añadir modelos*: Hugging Face, Ollama u Ollama local, ver
   [USO](USO.md#modelos-añadir-gestionar-y-ajustar)) o copia un `.gguf` a la carpeta de modelos del orquestador.
2. En el panel: elige el modelo → **Simular reparto** (mide la red y muestra qué irá a cada nodo) → **Cargar**.
3. Cuando el estado sea **READY**, prueba el chat del panel o la API. Ver [USO](USO.md).

Para una primera prueba usa un modelo pequeño (p. ej. 7–8B Q4_K_M, ~5 GB) y compáralo con un solo equipo.

## Elegir backend (CPU / GPU)

El asistente elige el **más rápido disponible** según el hardware (puedes cambiarlo en *Opciones avanzadas*, donde
cada opción explica para qué sirve). Con *auto*, si ese backend no se puede instalar o no arranca (driver antiguo,
falta un runtime), **prueba el siguiente** (CUDA → Vulkan → CPU) y lo indica. Después, el agente
**detecta solo su GPU** preguntando a llama.cpp, la presta con su VRAM real (también AMD e Intel) y, si el equipo
tiene varias, usa la de más memoria (la integrada se descarta si hay una dedicada). El asistente muestra al terminar
qué GPU ha detectado.

| Sistema | Hardware | Backend | Notas |
|---|---|---|---|
| Sistema | Hardware | Backend sugerido | Alternativas |
|---|---|---|---|
| Windows / Linux (x64 y ARM64) | NVIDIA, driver con CUDA ≥ 12.4 | `cuda` (12.x o 13.x según el driver, con su runtime) | `vulkan`, `cpu` |
| Windows / Linux | AMD | `vulkan` | `rocm` (experimental, necesita ROCm/HIP), `cpu` |
| Windows / Linux | Intel (Arc, iGPU) | `vulkan` | `sycl` (experimental, necesita oneAPI), `cpu` |
| Windows / Linux | NVIDIA con driver antiguo | `vulkan` | `cpu` |
| Windows / Linux | Sin GPU | `cpu` | |
| macOS Apple Silicon | — | `metal` | La memoria unificada cuenta como VRAM |
| macOS Intel | — | `cpu` | |

Todos los equipos usan la misma versión (`bNNNN`) aunque cada uno tenga un backend distinto: un nodo Linux con CUDA y
otro Windows con Vulkan funcionan juntos.

**Windows 11 con Control inteligente de aplicaciones (Smart App Control):** bloquea las DLL sin firmar de llama.cpp
(error `0xc0e90002` y aviso de Defender «Se ha bloqueado parte de esta aplicación»). Desactívalo en Seguridad de
Windows → Control de aplicaciones y navegador antes de instalar; el asistente lo detecta y lo explica.

**CUDA en Linux** se descarga ya compilado (paquete oficial `ubuntu-cuda`, con su runtime); solo necesitas el
driver de NVIDIA (`nvidia-smi` debe funcionar; en una VM, con la GPU en *passthrough*). Para compilarlo tú:
`LLAMA_REF=b1234 ./build-llamacpp.sh cuda` (misma etiqueta que el orquestador) y *Usar llama.cpp ya instalado*.

**Varias GPUs en un equipo:** se presta una (la de más memoria). Para elegir otra: `dllm agent --device CUDA1`, o
`"device"` en la configuración del agente.

**Ejemplo: dos PCs Windows con RTX 3060 (12 GB) y RX 7600 XT (16 GB).** Instala el orquestador en uno de ellos con
el rol **Ambos** (o en un tercer equipo como *Orquestador*) y el otro como **Nodo**. El de NVIDIA usará CUDA y el de
AMD Vulkan, detectados solos. Entre los dos aportan ~24 GiB de VRAM: suficiente para un 32B en Q4_K_M con ~8k de
contexto o un 27B con más. Si el modelo cabe en una sola GPU, DLLM usa solo esa. Conecta ambos por cable.

## Instalación desatendida (sin preguntas)

```bash
# orquestador que también es nodo, API accesible desde la red
python dllm.pyz setup --tui --yes --role both --models-dir ~/models --expose
# nodo
python dllm.pyz setup --tui --yes --role node --join DLLM1-xxxxxxxx
```

Opciones útiles de `setup`: `--backend cpu|vulkan|cuda|rocm|metal`, `--bin-dir CARPETA`, `--name NOMBRE`,
aporte del nodo (`--share 33`, `--mem-gb N`, `--threads N`, `--limits-mode auto|manual`; si das algún límite sin
`--limits-mode`, queda en manual), `--no-service`, puertos (`--api-port`, `--agent-port`, `--rpc-port`, `--beacon-port`), `--dry-run`
(simula sin escribir nada). Lista completa: `python dllm.pyz setup --help`.

## Sin Internet

1. En un equipo con Internet descarga el paquete de llama.cpp de su página de releases (la misma etiqueta para todos)
   y descomprímelo, o compílalo con `build-llamacpp.sh`.
2. Cópialo a cada equipo y en el asistente indica esa carpeta en *Usar llama.cpp ya instalado* (`--bin-dir`).
   Debe contener `llama-server` (orquestador) y/o `ggml-rpc-server` (nodos; `rpc-server` en builds antiguas de llama.cpp, también vale).

## Abrir el panel

El asistente crea un acceso directo **DLLM** (escritorio y menú Inicio en Windows; menú de aplicaciones en Linux) que
ejecuta `dllm open`: arranca DLLM si está parado, abre el panel en el navegador y **esconde la consola**. Si cierras
el navegador con DLLM en marcha, la consola vuelve a aparecer y ofrece **[A] abrir de nuevo**, **[P] parar DLLM** o
salir dejándolo funcionando. En el paquete también está `Open-DLLM.bat`. El asistente hace lo mismo: su consola se
esconde mientras está abierto y reaparece si cierras la pestaña sin terminar.

**Sin contraseña en el propio equipo:** el panel no pide nada desde el orquestador. Si activaste *Permitir usar la
API desde otros equipos*, desde ellos sí hace falta la API key (es la regla de seguridad del proyecto: nada escucha en
la red sin autenticación), pero no hay que escribirla: al terminar, el asistente da un **enlace para otros equipos**
(`http://IP:8080/#key=…`) que se abre una vez y el navegador la recuerda. También lo da el botón *API key* del panel
abierto en el orquestador.

## Cortafuegos

**Windows:** el asistente crea las reglas solo (casilla *Abrir en el cortafuegos…*, activada por defecto; desactívala o
usa `--no-firewall` para hacerlo a mano). Windows pide permiso de administrador **una vez**. Las reglas se llaman
`DLLM-agente`, `DLLM-beacon` y `DLLM-API`, se limitan al ejecutable de Python que usa DLLM, a los puertos indicados y
a la IP del orquestador (nodo) o a la subred local (orquestador), y se borran al desinstalar. Si se deniega el permiso,
el asistente muestra los comandos equivalentes. **Linux y macOS:** no se toca nada (requiere `sudo`); al terminar se
muestran los comandos exactos.

En el rol *Ambos* el agente local solo escucha en `127.0.0.1`, así que no necesita reglas. Puertos por defecto:

| Equipo | Puerto | Para qué | Abrir a |
|---|---|---|---|
| Nodo | TCP 8765 | API de control del agente | Solo la IP del orquestador |
| Nodo | TCP 50051 | Datos del modelo (proxy RPC autenticado) | Solo la IP del orquestador |
| Orquestador | UDP 50999 | Descubrimiento de nodos | La subred de los nodos |
| Orquestador | TCP 8080 | Panel y API | Solo si usas `--expose`, y a quien la use |

Ejemplo Linux (nodo, orquestador en 192.168.1.10):
```bash
sudo ufw allow from 192.168.1.10 to any port 8765 proto tcp
sudo ufw allow from 192.168.1.10 to any port 50051 proto tcp
```
En macOS, si aparece el aviso de *permitir conexiones entrantes* para Python, permítelo. En Windows, con las reglas de
DLLM creadas no debería aparecer el aviso de Defender; si lo aceptaste con una versión anterior, Windows creó reglas
amplias para `python.exe`/`pythonw.exe` (todos los puertos): puedes borrarlas en *Firewall de Windows Defender →
Configuración avanzada → Reglas de entrada* y dejar solo las `DLLM-*`.

## Dónde queda instalado

| Qué | Ruta |
|---|---|
| Configuración (secreto incluido) | `~/.dllm/config.json` |
| Programa | `~/.dllm/dllm.pyz` |
| llama.cpp descargado | `<carpeta de DLLM>/llama.cpp/llama-<etiqueta>-<backend>/` (instalaciones ≤ 0.3.5: `~/.dllm/llama/`) |
| Modelos (orquestador) | `<carpeta de DLLM>/models/` (o la carpeta de modelos que ya tuvieras) |
| Reglas del cortafuegos (Windows) | `DLLM-agente`, `DLLM-beacon`, `DLLM-API` |
| Servicio Linux | `~/.config/systemd/user/dllm.service` (`systemctl --user status dllm`) |
| Servicio macOS | `~/Library/LaunchAgents/com.dllm.plist` (registro en `~/.dllm/dllm.log`) |
| Servicio Windows | Tarea programada `DLLM` (al iniciar sesión) |

En Windows `~` es `%USERPROFILE%`. Para que en Linux arranque sin iniciar sesión:
`sudo loginctl enable-linger $USER`.

## Actualizar

Descarga el paquete nuevo y vuelve a ejecutar el asistente **en el orquestador**. Conserva su secreto, así que los
nodos no necesitan un código nuevo. Si cambia la versión de llama.cpp, **los nodos se actualizan solos**: el
orquestador les pide instalar la misma (paquete oficial, sha256 verificado). Para actualizar también el programa DLLM
de un nodo, vuelve a ejecutar el asistente en él.

## Desinstalar

Sirve igual para un nodo, un orquestador o ambos:

- **Desde el paquete:** doble clic en `Uninstall-DLLM.bat` (Windows) o `Uninstall-DLLM.command` (macOS), o
  `./uninstall.sh` (Linux). Pregunta qué borrar.
- **Desde el asistente:** si DLLM ya está instalado, la primera pantalla muestra **Desinstalar** (en terminal, opción 4).
- **Por línea de comandos:**

```bash
dllm uninstall                   # para el programa, quita el arranque automático y las reglas del cortafuegos
dllm uninstall --purge           # además borra llama.cpp y ~/.dllm (configuración y secreto)
dllm uninstall --purge --models  # además borra los modelos, solo si están en <carpeta de DLLM>/models
```

Los modelos se conservan salvo que lo pidas, y una carpeta de modelos que no creó DLLM nunca se borra.

Donde `dllm` equivale a `python ~/.dllm/dllm.pyz` (Windows: `py -3 %USERPROFILE%\.dllm\dllm.pyz`).
