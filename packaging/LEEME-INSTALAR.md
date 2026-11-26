# Instalar DLLM

Este paquete es **el mismo en todos los equipos**. El asistente te pregunta qué papel tendrá cada uno.

## Antes de empezar
- Python 3.9+ (los lanzadores te ofrecen instalarlo si falta).
- Los equipos en la misma red local, idealmente a **1 Gbps o más**.
- Internet en la instalación (descarga llama.cpp de las releases oficiales). Sin Internet: `--bin-dir CARPETA`
  con llama.cpp ya instalado (debe incluir `llama-server` y `ggml-rpc-server`, o `rpc-server` si es una build antigua).
- **Windows 11:** si está activo el *Control inteligente de aplicaciones*, bloquea llama.cpp (sus DLL no van
  firmadas). Desactívalo en Seguridad de Windows → Control de aplicaciones y navegador antes de instalar.

## Con una sola orden (sin descargar el paquete)

- Linux/macOS: `bash <(curl -fsSL https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.sh)`
- Windows (PowerShell): `irm https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.ps1 | iex`

## En 3 pasos

| Sistema | Cómo abrir el asistente |
|---|---|
| Windows | Doble clic en **Install-DLLM.bat** |
| macOS | Doble clic en **Install-DLLM.command** (si lo bloquea: clic derecho → Abrir) |
| Linux | `./install.sh` (en terminal sin escritorio abre el asistente de texto; fuerza con `--tui` o `--gui`) |

1. **En el equipo principal** → elige *Orquestador* (o *Ambos* si también quieres que preste su CPU/GPU).
   Al terminar te muestra un **código de unión** (`DLLM1-…`) y la dirección del panel.
2. **En cada equipo que quieras sumar** → abre el mismo asistente, elige *Nodo*, pega el código y elige cuánto
   comparte: *Automático* (lo gestiona el orquestador desde su panel) o *Limitar desde este equipo* (p. ej. 33 %).
3. Abre el panel del orquestador, comprueba que ves los nodos, añade un modelo en **Añadir modelos** (Hugging Face u
   Ollama), elígelo y pulsa **Cargar**.

El código de unión es una **contraseña del clúster**: no lo publiques. Los nodos solo aceptan conexiones del orquestador.

## Después
```
dllm doctor              # diagnóstico local
dllm join-code           # volver a ver el código de unión (en el orquestador)
dllm uninstall [--purge] # desinstalar (con --purge borra también configuración y llama.cpp)
```
(`dllm` = `python ~/.dllm/dllm.pyz`; en Windows `py -3 %USERPROFILE%\.dllm\dllm.pyz`.)

## Notas
- **Carpeta de DLLM** (por defecto `~/DLLM`): ahí van llama.cpp y los modelos. La configuración y el secreto quedan en `~/.dllm`.
- **Cortafuegos:** en Windows el asistente abre solo los puertos necesarios, limitados a tu red (pide permiso de
  administrador una vez). En Linux/macOS te muestra los comandos exactos.
- **Nodo sin código de unión:** déjalo vacío en el asistente y reclámalo desde el panel del orquestador (Nodos →
  Equipos esperando unirse); confirma en el nodo el código de verificación. Si no aparece, indica la IP del
  orquestador en el asistente del nodo, o usa Nodos → Añadir por IP / Buscar en la red en el orquestador.
- **Abrir el panel:** acceso directo *DLLM* (o `Open-DLLM.bat`); la consola se esconde y reaparece si cierras el navegador.
- **Desinstalar:** `Uninstall-DLLM.bat` (Windows), `Uninstall-DLLM.command` (macOS) o `./uninstall.sh` (Linux): programa aparte del
  instalador (en un orquestador ofrece guardar antes una copia de la configuración).
- **Recuperar un orquestador:** instálalo y en el primer paso elige *Restaurar desde una copia* (fichero `.dllmbak`).
- Todos los equipos usan la **misma versión de llama.cpp**: la fija el orquestador en el código de unión.
- Linux con GPU NVIDIA: se usa el paquete oficial CUDA (driver con CUDA ≥ 12.4); si no es posible, el asistente prueba
  Vulkan y luego CPU. Para una compilación propia: `./build-llamacpp.sh cuda` y `--bin-dir`.
- El tráfico entre nodos va **autenticado pero sin cifrar**: úsalo en una red de confianza o una VPN.

## Más información
Documentación completa (uso, API, seguridad, solución de problemas): https://github.com/dasehe/dllm/tree/main/docs

## Licencia
DLLM — idea original y desarrollo inicial de David Serrano Hernando. Software libre bajo GNU AGPL-3.0-or-later
(ver `LICENSE` y `NOTICE`).
