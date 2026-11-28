# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Copias de seguridad programadas y siempre cifradas.

Cada `every_hours` horas crea una copia `.dllmbak` cifrada (PBKDF2-HMAC-SHA256 + HMAC, como las manuales) en la
carpeta elegida, con permisos 600, y conserva las `keep` más recientes (solo borra copias suyas: `dllm-*.dllmbak`
creadas por la programación). La frase de cifrado se guarda en `~/.dllm/backup_schedule.json` (permisos 600) para poder
trabajar sin nadie delante; nunca se devuelve por la API. Protege las copias que se lleven a otro sitio (NAS, nube):
quien tenga acceso a la cuenta del orquestador ya tiene la configuración en claro."""
import json
import os
import re
import threading
import time
from pathlib import Path

from . import backup

DEFAULTS = {"enabled": False, "every_hours": 24, "keep": 7, "dir": "", "include_logs": False, "passphrase": ""}
_FILE = re.compile(r"^dllm-auto-[\w-]{1,40}-\d{8}-\d{6}\.dllmbak$")


class ScheduleError(ValueError):
    pass


class BackupScheduler:
    def __init__(self, home, make_blob, event=None, host="dllm"):
        """`make_blob(passphrase, include_logs)` devuelve los bytes de la copia; `event(msg, level)` registra."""
        self.home = Path(home)
        self.path = self.home / "backup_schedule.json"
        self.make_blob, self.event = make_blob, event or (lambda *a, **k: None)
        self.host = "".join(c for c in (host or "dllm") if c.isalnum() or c in "-_")[:30] or "dllm"
        self.lock = threading.Lock()
        self.running = False
        self.data = self._load()

    def default_dir(self):
        return str(self.home / "scheduled-backups")

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            d = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            d = {}
        out = dict(DEFAULTS)
        out.update({k: d[k] for k in DEFAULTS if k in d})
        out["last"] = d.get("last")
        return out

    def reload(self):
        with self.lock:
            self.data = self._load()

    def _save(self):
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def view(self):
        with self.lock:
            d = {k: v for k, v in self.data.items() if k != "passphrase"}
            d["has_passphrase"] = bool(self.data.get("passphrase"))
            d["dir"] = d.get("dir") or self.default_dir()
            d["default_dir"] = self.default_dir()
            d["next"] = self._next_time()
            d["running"] = self.running
            d["files"] = self._files_view()
            return d

    def _next_time(self):
        if not self.data.get("enabled"):
            return None
        last = (self.data.get("last") or {}).get("t") or 0
        return max(time.time(), last + self.data["every_hours"] * 3600) if last else time.time()

    def _dir(self):
        return Path(self.data.get("dir") or self.default_dir())

    def _files(self):
        try:
            return sorted((p for p in self._dir().iterdir() if _FILE.match(p.name) and p.is_file()),
                          key=lambda p: p.name)
        except OSError:
            return []

    def _files_view(self):
        out = []
        for p in reversed(self._files()):
            try:
                out.append({"name": p.name, "size": p.stat().st_size, "t": int(p.stat().st_mtime)})
            except OSError:
                pass
        return out

    def configure(self, upd):
        if not isinstance(upd, dict):
            raise ScheduleError("petición no válida")
        with self.lock:
            d = dict(self.data)
            for k, v in upd.items():
                if k == "enabled" or k == "include_logs":
                    d[k] = bool(v)
                elif k in ("every_hours", "keep"):
                    lo, hi = (1, 720) if k == "every_hours" else (1, 100)
                    try:
                        v = int(v)
                    except (TypeError, ValueError):
                        raise ScheduleError(f"{k}: debe ser un número entero")
                    if not lo <= v <= hi:
                        raise ScheduleError(f"{k}: debe estar entre {lo} y {hi}")
                    d[k] = v
                elif k == "dir":
                    v = str(v or "").strip()
                    if v and (len(v) > 500 or "\0" in v or not os.path.isabs(os.path.expanduser(v))):
                        raise ScheduleError("la carpeta debe ser una ruta absoluta")
                    d[k] = os.path.expanduser(v) if v else ""
                elif k == "passphrase":
                    if v in (None, ""):
                        continue  # vacío = conservar la actual
                    if not isinstance(v, str) or not 8 <= len(v) <= 256:
                        raise ScheduleError("la contraseña de cifrado debe tener entre 8 y 256 caracteres")
                    d[k] = v
                else:
                    raise ScheduleError(f"ajuste desconocido: {k}")
            if d["enabled"] and not d.get("passphrase"):
                raise ScheduleError("las copias programadas van siempre cifradas: indica una contraseña de cifrado")
            if d["enabled"]:
                try:
                    p = Path(d.get("dir") or self.default_dir())
                    p.mkdir(parents=True, exist_ok=True, mode=0o700)
                    probe = p / ".dllm-write-test"
                    probe.write_bytes(b"")
                    probe.unlink()
                except OSError as e:
                    raise ScheduleError(f"no se puede escribir en la carpeta: {e}")
            self.data = d
            self._save()
        return self.view()

    def due(self, now=None):
        with self.lock:
            if not self.data.get("enabled") or self.running:
                return False
            last = (self.data.get("last") or {}).get("t") or 0
            return (now or time.time()) - last >= self.data["every_hours"] * 3600

    def run_now(self):
        """Crea una copia ahora (programada o con «Hacer copia ahora»). Devuelve el resultado."""
        with self.lock:
            if self.running:
                raise ScheduleError("ya se está haciendo una copia")
            if not self.data.get("passphrase"):
                raise ScheduleError("las copias programadas van siempre cifradas: indica una contraseña de cifrado")
            self.running = True
            pw, logs, keep, folder = self.data["passphrase"], self.data["include_logs"], self.data["keep"], self._dir()
        res = {"t": time.time(), "ok": False}
        try:
            blob = self.make_blob(pw, logs)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = f"dllm-auto-{self.host}-{time.strftime('%Y%m%d-%H%M%S')}{backup.EXT}"
            target = folder / name
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            res.update(ok=True, file=str(target), size=len(blob))
            removed = 0
            files = self._files()
            for old in files[:max(0, len(files) - keep)]:
                try:
                    old.unlink()
                    removed += 1
                except OSError:
                    pass
            res["removed"] = removed
            self.event(f"copia programada creada (cifrada): {target} ({len(blob) // 1024} KiB)", "info")
        except Exception as e:  # cualquier fallo queda registrado (y llega a las alertas)
            res["error"] = str(e)[:300]
            self.event(f"error en la copia programada: {res['error']}", "error")
        finally:
            with self.lock:
                self.running = False
                self.data["last"] = {k: res[k] for k in ("t", "ok", "file", "size", "error") if k in res}
                try:
                    self._save()
                except OSError:
                    pass
        return res

    def loop(self, interval=60, stop=None):
        while not (stop and stop.is_set()):
            try:
                if self.due():
                    self.run_now()
            except Exception:
                pass
            time.sleep(interval)
