# Cómo contribuir

¡Gracias por querer mejorar DLLM! Este documento explica cómo enviar cambios y en qué condiciones.

## Licencia de las contribuciones

DLLM es software libre bajo la **GNU AGPL-3.0-or-later** con los términos adicionales de atribución
descritos en [`NOTICE`](NOTICE). Al contribuir:

- Tu aportación se publica bajo **esa misma licencia**. No hay cesión de derechos ni CLA: **conservas la
  autoría** de lo que escribas, igual que el creador del proyecto conserva la suya.
- Como ninguna persona concentra todos los derechos, **nadie puede cerrar el proyecto ni cambiarle la licencia
  en solitario**: DLLM seguirá siendo libre lo continúe quien lo continúe.
- Debes mantener intactos los avisos de copyright, las cabeceras `SPDX` y el fichero `NOTICE`.

### Firma DCO (obligatoria)

Usamos el [Developer Certificate of Origin 1.1](https://developercertificate.org/). Firmando un commit
certificas, en resumen, que tienes derecho a enviar ese código y que aceptas que se publique bajo la licencia
del proyecto. Se firma añadiendo `-s` al hacer commit:

```bash
git commit -s -m "Describe el cambio"
# añade la línea: Signed-off-by: Tu Nombre <tu@correo>
```

No se aceptan commits sin `Signed-off-by` ni código copiado de proyectos con licencias incompatibles.

## Flujo de trabajo

1. Abre un *issue* describiendo el problema o la propuesta antes de cambios grandes.
2. Crea una rama desde `main`, haz tus cambios y abre un *pull request* hacia `main`.
3. Requisitos del PR:
   - `python -m unittest discover -s tests` en verde (la CI lo comprueba en Linux, macOS y Windows).
   - Tests nuevos para el comportamiento nuevo.
   - Documentación actualizada en el mismo PR: `README.md`, el documento afectado de `docs/` y la sección
     `[Sin publicar]` de `CHANGELOG.md`.
   - Ficheros nuevos de código con la cabecera:
     ```python
     # SPDX-License-Identifier: AGPL-3.0-or-later
     # Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
     ```
   - Añádete a [`AUTHORS.md`](AUTHORS.md).

## Normas técnicas

- `dllm/` usa **solo la biblioteca estándar** de Python ≥ 3.9: sin dependencias externas.
- Seguridad primero: nada escucha fuera de loopback sin autenticación; `rpc-server` nunca se expone.
- Mensajes al usuario y documentación en **español**, claros y con la acción a tomar.
- Más detalles en [docs/DESARROLLO.md](docs/DESARROLLO.md).

## Seguridad

No publiques vulnerabilidades en *issues*: contacta directamente con el propietario del repositorio
(ver [docs/SEGURIDAD.md](docs/SEGURIDAD.md)).
