# Desarrollo

## Estructura

```
dllm/                 paquete Python (solo biblioteca estándar)
  cli.py              punto de entrada y subcomandos
  agent.py            agente de nodo
  orchestrator.py     orquestador, API, recuperación
  scheduler.py        reparto
  gguf.py  hw.py      metadatos de modelos, hardware
  auth.py  util.py    HMAC, handshake, túnel
  config.py           config.json y código de unión
  installer.py        descarga de llama.cpp, servicios, diagnóstico
  wizard.py           asistente (terminal y web)
  pairing.py          emparejamiento de nodos sin código (DH + código de verificación)
  presence.py         pestañas abiertas y consola escondida (`dllm open`, asistente)
  models.py telemetry.py devices.py limits.py   modelos, monitor, dispositivos, límites
  ui.html  setup.html panel y asistente web
packaging/            lanzadores y LEEME del paquete distribuible
scripts/              build_package.py, build-llamacpp.sh, ejemplo de unidad systemd
tests/                unittest + binarios simulados (mockbin/) + generador de GGUF falsos
docs/                 documentación
.github/workflows/    tests.yml (CI, manual) · release.yml (publicación)
```

## Entorno

```bash
git clone https://github.com/dasehe/dllm && cd dllm
python -m dllm --help          # no hay dependencias que instalar
```

Para probar sin llama.cpp real, apunta `--bin-dir` a `tests/mockbin` (simulan `rpc-server` y `llama-server`).
Con llama.cpp real: `./scripts/build-llamacpp.sh cpu` y `--bin-dir llama.cpp/build/bin`.

Dos "equipos" en la misma máquina: usa `DLLM_HOME` distinto y puertos distintos:
```bash
DLLM_HOME=/tmp/a python -m dllm setup --tui --yes --role both --no-service --bin-dir tests/mockbin
DLLM_HOME=/tmp/b python -m dllm setup --tui --yes --role node --no-service --bin-dir tests/mockbin \
  --join "$(DLLM_HOME=/tmp/a python -m dllm join-code)" --agent-port 8766 --rpc-port 50052
DLLM_HOME=/tmp/a python -m dllm run & DLLM_HOME=/tmp/b python -m dllm run &
```

## Tests

```bash
python -m unittest discover -s tests -v
```

| Archivo | Cubre |
|---|---|
| `test_core.py` | GGUF, scheduler, HMAC, handshake, beacons |
| `test_e2e.py` | 3 agentes + orquestador: reparto, túneles, API, streaming, autenticación, recuperación, métricas (POSIX) |
| `test_setup.py` | Selección de paquetes, extracción segura, sha256, servicios, código de unión, asistente web, instalación completa con el `.pyz` (Linux) |

La tabla recoge los principales; el resto están en `tests/`. Los tests se ejecutan **en local** antes de cada commit.
El workflow `tests` (Linux, macOS y Windows con Python 3.9 y 3.13, y paquete como artefacto) es **solo manual**:
**Actions → tests → Run workflow**. No se lanza en push ni en pull requests.

Variables útiles: `DLLM_HOME` (carpeta de configuración), `DLLM_BIN_DIR`, `DLLM_RELEASE_FILE` (JSON con formato de
la API de releases de GitHub, para instalar sin Internet o en tests).

## Construir el paquete

```bash
python scripts/build_package.py            # → dist/dllm-package-<versión>/ y .zip (+ SHA256SUMS)
```
Empaqueta `dllm/` en un `dllm.pyz` (zipapp) y añade los lanzadores de `packaging/`.

## Publicar una versión

1. Actualiza la versión en `dllm/__init__.py` **y** `pyproject.toml`.
2. Añade la sección `## [X.Y.Z] - AAAA-MM-DD` en `CHANGELOG.md` (es el texto de la release).
3. Actualiza la documentación afectada.
4. `python -m unittest discover -s tests` en verde.
5. Commit y push a `main`.
6. En GitHub: **Actions → release → Run workflow** (rama `main`). El workflow toma la versión de `dllm/__init__.py`,
   comprueba que no exista ya esa release, pasa los tests, construye el paquete y crea la etiqueta `vX.Y.Z` y la
   release con el `.zip`, `SHA256SUMS` y la sección del CHANGELOG como notas.
   Alternativa equivalente: `git tag vX.Y.Z && git push origin vX.Y.Z` (la etiqueta debe coincidir con la versión).

**Versiones pequeñas (desde 0.7.0):** cada cambio o grupo pequeño de cambios se publica con su propia versión: corrección
de un fallo o ajuste mínimo → versión de parche (0.7.1, 0.7.2…); funcionalidad nueva → versión menor (0.8.0). No se acumulan
cambios en `[Sin publicar]` más de lo necesario: cada commit que cambia el comportamiento sube la versión (en
`dllm/__init__.py`, `pyproject.toml` y la sección del CHANGELOG) y avisa de que se puede pulsar *Run workflow*.

Versionado semántico: cambios incompatibles en configuración, código de unión o protocolo del agente → versión menor
mientras estemos en 0.x, y documentarlo en el CHANGELOG con instrucciones de actualización.

## Idiomas (inglés por defecto, Español de España opcional)

Las páginas (`ui.html`, `setup.html`, `uninstall.html`) se escriben en español y se muestran en **inglés por defecto**: el
servidor inserta en el marcador `<!--I18N-->` el diccionario `dllm/i18n/en.json` (español → inglés), las expresiones de
`dllm/i18n/en.re.json` (textos con valores, p. ej. «6.0 GiB libres») y `dllm/i18n/runtime.js`, que traduce el DOM y los
`alert/confirm`. El usuario elige idioma con el selector de la cabecera (se recuerda en el navegador; `?lang=es` también).

- **Cada texto visible nuevo necesita su entrada en `en.json`**: `python scripts/i18n_extract.py --json` lista los que faltan
  y `tests/test_i18n.py` falla si queda alguno.
- Textos ambiguos: `data-en="Unload"` en el elemento fija la traducción (p. ej. «Descargar» = descargar / sacar de memoria).
- Contenido del usuario (chat, código): `class="notr"`, no se traduce.

## Licencia en el código

- Todo fichero de código empieza por:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  # Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
  ```
  `tests/test_license.py` falla si falta.
- Los avisos legales que muestran las interfaces salen de `dllm/__init__.py` (`ATTRIBUTION`, `COPYRIGHT`,
  `SOURCE_URL`, `legal_notice()`) y del pie de `ui.html`/`setup.html`. No los elimines: la sección 7.b de la
  licencia exige conservar la atribución, y la 13 ofrecer el código fuente a quien use el servicio por red.
- Commits firmados con `git commit -s` (DCO). Ver [CONTRIBUTING.md](../CONTRIBUTING.md).

## Convenciones

- Sin dependencias externas en `dllm/` (debe funcionar con un Python recién instalado).
- Mensajes al usuario en español, claros y con la acción a tomar.
- Cualquier entrada de red se valida antes de usarse; ninguna escucha fuera de loopback sin autenticación.
- Cada cambio visible actualiza `docs/` y `CHANGELOG.md` en el mismo commit.
