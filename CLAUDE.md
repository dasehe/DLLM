# Instrucciones para trabajar en este repositorio

DLLM (Distributed LLM): orquestador de inferencia distribuida sobre llama.cpp RPC. Propietario: David Serrano.
Idioma del proyecto: **español** (código comentado, mensajes, documentación, commits).

## Reglas

- Todo el trabajo se integra en `main` de `dasehe/dllm`; las versiones se publican desde aquí.
- **Cada cambio visible actualiza la documentación en el mismo commit**: `README.md`, el documento afectado de
  `docs/` y la sección `[Sin publicar]` de `CHANGELOG.md`. Si cambia la instalación, también `packaging/LEEME-INSTALAR.md`.
- Antes de hacer commit: `python -m unittest discover -s tests` en verde. Añade tests para el comportamiento nuevo.
- `dllm/` usa solo la biblioteca estándar (Python ≥ 3.9). No añadir dependencias.
- Seguridad: nada escucha fuera de loopback sin autenticación; `rpc-server` nunca se expone directamente.
- No subir modelos, binarios de llama.cpp, secretos ni `config.json`.
- Licencia AGPL-3.0-or-later con atribución a David Serrano Hernando (`NOTICE`): cabecera SPDX en cada fichero
  de código nuevo; no quitar los avisos legales de las interfaces; commits con `git commit -s` (DCO).

## Publicar una versión

**Versiones pequeñas:** cada cambio (aunque sea un arreglo mínimo) sube la versión: parche = fallo/ajuste pequeño, menor =
funcionalidad nueva. Un commit por versión, con su sección en `CHANGELOG.md`.

Ver `docs/DESARROLLO.md#publicar-una-versión`: versión en `dllm/__init__.py` y `pyproject.toml`, sección en
`CHANGELOG.md`, tests, push a `main` y **Actions → release → Run workflow** (o etiqueta `vX.Y.Z`). El workflow
`release.yml` construye y publica. Desde las sesiones de Claude el proxy de git no deja subir etiquetas: usar el botón.

## Comprobar

- Tests: `python -m unittest discover -s tests -v`
- Paquete: `python scripts/build_package.py --out dist`
- CI: `.github/workflows/tests.yml` (Linux/macOS/Windows), **solo manual**. No hay tests automáticos al subir: no
  esperar a la CI ni volver a activar disparadores `push`/`pull_request` sin pedirlo el propietario.
