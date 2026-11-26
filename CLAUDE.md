# Instrucciones para trabajar en este repositorio

DLLM (Distributed LLM): orquestador de inferencia distribuida sobre llama.cpp RPC. Propietario: David Serrano.
**Idiomas:** todo lo que ve el usuario (panel, instaladores, desinstalador, terminal, mensajes de la API y documentación)
está **en inglés por defecto con opción Español (España)**. Cada versión nueva mantiene los dos idiomas al día: ninguna
cadena visible ni documento nuevo se publica solo en un idioma (textos de interfaz en los diccionarios de `dllm/i18n/`;
documentación en `README.md` + `README.es.md` y `docs/` + `docs/es/`). Comentarios del código y commits: español.

## Reglas

- Todo el trabajo se integra en `main` de `dasehe/dllm`; las versiones se publican desde aquí.
- **Cada cambio visible actualiza la documentación en el mismo commit**, en inglés y en español: `README.md`/`README.es.md`,
  el documento afectado de `docs/` y `docs/es/`, y la sección de la versión en `CHANGELOG.md`. Si cambia la instalación, también `packaging/README-INSTALL.md` y `packaging/LEEME-INSTALAR.md`.
- Antes de hacer commit: `python -m unittest discover -s tests` en verde. Añade tests para el comportamiento nuevo.
- `dllm/` usa solo la biblioteca estándar (Python ≥ 3.9). No añadir dependencias.
- Seguridad: nada escucha fuera de loopback sin autenticación; `rpc-server` nunca se expone directamente.
- No subir modelos, binarios de llama.cpp, secretos ni `config.json`.
- Licencia AGPL-3.0-or-later con atribución a David Serrano Hernando (`NOTICE`): cabecera SPDX en cada fichero
  de código nuevo; no quitar los avisos legales de las interfaces; commits con `git commit -s` (DCO).

## Publicar una versión

**Versiones pequeñas:** cada cambio (aunque sea un arreglo mínimo) sube la versión: parche = fallo/ajuste pequeño, menor =
funcionalidad nueva. Un commit por versión, con su sección en `CHANGELOG.md`.

Ver `docs/DEVELOPMENT.md#releasing-a-version` (en español: `docs/es/DESARROLLO.md`): versión en `dllm/__init__.py` y `pyproject.toml`, sección en
`CHANGELOG.md`, tests, push a `main` y **Actions → release → Run workflow** (o etiqueta `vX.Y.Z`). El workflow
`release.yml` construye y publica. Desde las sesiones de Claude el proxy de git no deja subir etiquetas: usar el botón.

## Comprobar

**Tests siempre en local** antes de cada versión (`python -m unittest discover -s tests`, más la prueba en navegador de lo
que cambie en las páginas). El propietario no quiere ejecuciones de GitHub Actions (salen en rojo por falta de facturación
en su cuenta): no lanzar workflows de tests. Ojo: ejecuciones anteriores sí corrieron y fallaron por los propios tests en
Windows/macOS, así que un rojo **no** se ignora por defecto. Al informar, decir en qué sistema y Python se probó (aquí:
Linux); Windows y macOS solo los puede probar el propietario.

- Tests: `python -m unittest discover -s tests -v`
- Paquete: `python scripts/build_package.py --out dist`
- CI: `.github/workflows/tests.yml` (Linux/macOS/Windows), **solo manual**. No hay tests automáticos al subir: no
  esperar a la CI ni volver a activar disparadores `push`/`pull_request` sin pedirlo el propietario.
