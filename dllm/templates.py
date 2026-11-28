# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Plantillas de despliegue: guardar una forma de cargar un modelo (modelo, contexto, dónde ejecutarlo con sus
deslizantes y prioridades, fijado, sustituir o añadir) y repetirla con un clic o `dllm template apply NOMBRE`.
Se guardan en `~/.dllm/templates.json` (van en las copias de seguridad)."""
import json
import os
import re
import threading
import time
from pathlib import Path

MAX_TEMPLATES = 100
_NAME = re.compile(r"^[\w .@()+-]{1,64}$")


class TemplateError(ValueError):
    pass


class Templates:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        if not self.path:
            return {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d.get("templates", {}) if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def reload(self):
        with self.lock:
            self.data = self._load()

    def _save(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"templates": self.data}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def view(self):
        with self.lock:
            return {"templates": dict(sorted(self.data.items()))}

    def get(self, name):
        with self.lock:
            t = self.data.get(str(name or ""))
        if t is None:
            raise TemplateError("plantilla no encontrada")
        return dict(t)

    def save(self, req, validate_targets=None):
        """Crea o sustituye una plantilla. `validate_targets` comprueba el formato de los destinos."""
        if not isinstance(req, dict):
            raise TemplateError("petición no válida")
        name = str(req.get("name") or "").strip()
        if not _NAME.match(name):
            raise TemplateError("nombre de plantilla no válido (máx. 64: letras, números, espacio . _ @ ( ) + -)")
        model = req.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 300:
            raise TemplateError("indica el modelo de la plantilla")
        try:
            ctx = int(req.get("ctx") or 0) or None
        except (TypeError, ValueError):
            raise TemplateError("contexto no válido")
        if ctx is not None and not 256 <= ctx <= 1 << 21:
            raise TemplateError("contexto no válido (entre 256 y 2097152)")
        slot = req.get("slot", "new")
        if slot not in ("new", 0, "0"):
            raise TemplateError("destino no válido: «new» (añadir) o 0 (sustituir el principal)")
        targets = req.get("targets") or []
        if validate_targets:
            validate_targets(targets)
        t = {"model": model.strip(), "ctx": ctx, "slot": "new" if slot == "new" else 0,
             "pinned": req.get("pinned", True) is not False, "targets": targets,
             "note": str(req.get("note") or "")[:200], "updated": int(time.time())}
        with self.lock:
            if name not in self.data and len(self.data) >= MAX_TEMPLATES:
                raise TemplateError(f"máximo {MAX_TEMPLATES} plantillas")
            self.data[name] = t
            self._save()
        return name, t

    def delete(self, name):
        with self.lock:
            if self.data.pop(str(name or ""), None) is None:
                raise TemplateError("plantilla no encontrada")
            self._save()
