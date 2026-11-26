# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Usuarios, grupos, roles y API keys del orquestador (control de acceso por roles).

- Los **roles** son conjuntos de permisos; los **grupos** tienen roles; los **usuarios** pertenecen a grupos.
- `admin` no se puede borrar ni perder el rol de administrador (para no quedarse fuera del panel).
- Contraseñas con PBKDF2-HMAC-SHA256 (sal aleatoria, 310 000 iteraciones); API keys guardadas como SHA-256 (son
  aleatorias de 192 bits, no hace falta derivación lenta) y mostradas una sola vez.
- Sesiones del panel en memoria: token aleatorio en cookie HttpOnly, caducidad deslizante.
Todo en `~/.dllm/users.json` con permisos 600."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

PERMS = ("view", "chat", "models", "nodes", "system", "users", "audit")
PERM_INFO = {"view": "ver el panel, el monitor y los registros", "chat": "chat del panel y API (/v1, Ollama)",
             "models": "cargar, descargar y configurar modelos", "nodes": "añadir, reclamar, medir y configurar nodos",
             "system": "copias de seguridad, código de unión y ajustes del sistema",
             "users": "gestionar usuarios, grupos, roles y API keys", "audit": "ver el registro de auditoría"}
DEFAULT_ROLES = {"administrator": list(PERMS), "operator": ["view", "chat", "models", "nodes"],
                 "user": ["view", "chat"], "viewer": ["view"]}
DEFAULT_GROUPS = {"administrators": ["administrator"], "operators": ["operator"], "users": ["user"]}
ADMIN, ADMIN_GROUP, ADMIN_ROLE = "admin", "administrators", "administrator"
DEFAULT_PASSWORD = "admin"
ITERATIONS = 310_000
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
SESSION_TTL = 12 * 3600
KEY_PREFIX = "dllm_"


class UserError(ValueError):
    pass


def hash_password(pw, iterations=ITERATIONS, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, AttributeError):
        return False


def key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _name(n, what="nombre"):
    if not isinstance(n, str) or not NAME_RE.match(n):
        raise UserError(f"{what} no válido (letras, números, . _ @ -; máx. 64)")
    return n


def _password(pw):
    if not isinstance(pw, str) or len(pw) < 8 or len(pw) > 256:
        raise UserError("la contraseña debe tener entre 8 y 256 caracteres")
    return pw


class Users:
    def __init__(self, path, iterations=ITERATIONS):
        self.path = Path(path)
        self.iterations = iterations
        self.lock = threading.RLock()
        self.sessions = {}  # token → {"user", "exp", "ip"}
        self.data = self._load()

    # ------------------------------------------------------------------ persistencia
    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("roles", {k: {"perms": list(v)} for k, v in DEFAULT_ROLES.items()})
        d.setdefault("groups", {k: {"roles": list(v)} for k, v in DEFAULT_GROUPS.items()})
        d.setdefault("users", {})
        d.setdefault("keys", {})
        d["roles"].setdefault(ADMIN_ROLE, {"perms": list(PERMS)})
        d["roles"][ADMIN_ROLE]["perms"] = list(PERMS)  # el rol de administrador siempre lo tiene todo
        d["groups"].setdefault(ADMIN_GROUP, {"roles": [ADMIN_ROLE]})
        if ADMIN_ROLE not in d["groups"][ADMIN_GROUP]["roles"]:
            d["groups"][ADMIN_GROUP]["roles"].append(ADMIN_ROLE)
        if ADMIN not in d["users"]:
            d["users"][ADMIN] = {"pw": hash_password(DEFAULT_PASSWORD, self.iterations), "groups": [ADMIN_GROUP],
                                 "default_pw": True, "disabled": False, "created": int(time.time())}
        a = d["users"][ADMIN]
        a["disabled"] = False
        if ADMIN_GROUP not in a["groups"]:
            a["groups"].append(ADMIN_GROUP)
        return d

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------ permisos
    def perms_of(self, user):
        u = self.data["users"].get(user)
        if not u or u.get("disabled"):
            return set()
        out = set()
        for g in u.get("groups", []):
            for r in (self.data["groups"].get(g) or {}).get("roles", []):
                out |= set((self.data["roles"].get(r) or {}).get("perms", []))
        if user == ADMIN:
            out |= set(PERMS)
        return out

    # ------------------------------------------------------------------ autenticación
    def authenticate(self, user, pw):
        u = self.data["users"].get(user)
        if not u or u.get("disabled"):
            check_password(pw, hash_password("x", 1000))  # tiempo parecido aunque el usuario no exista
            return False
        return check_password(pw, u["pw"])

    def is_default_password(self, user):
        return bool((self.data["users"].get(user) or {}).get("default_pw"))

    def new_session(self, user, ip=""):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self._purge()
            self.sessions[tok] = {"user": user, "exp": time.time() + SESSION_TTL, "ip": ip}
        return tok

    def session_user(self, tok):
        if not tok:
            return None
        with self.lock:
            s = self.sessions.get(tok)
            if not s or s["exp"] < time.time():
                self.sessions.pop(tok, None)
                return None
            if not self.perms_of(s["user"]):  # usuario borrado o desactivado
                self.sessions.pop(tok, None)
                return None
            s["exp"] = time.time() + SESSION_TTL
            return s["user"]

    def end_session(self, tok):
        with self.lock:
            self.sessions.pop(tok, None)

    def _purge(self, user=None):
        now = time.time()
        for t in [t for t, s in self.sessions.items() if s["exp"] < now or (user and s["user"] == user)]:
            self.sessions.pop(t, None)

    def key_user(self, key):
        """API key → (usuario, id de la clave) o (None, None)."""
        if not key:
            return None, None
        h = key_hash(key)
        with self.lock:
            for kid, k in self.data["keys"].items():
                if hmac.compare_digest(k["hash"], h):
                    if not self.perms_of(k["user"]):
                        return None, None
                    if time.time() - k.get("last_used", 0) > 60:
                        k["last_used"] = int(time.time())
                        self.save()
                    return k["user"], kid
        return None, None

    # ------------------------------------------------------------------ gestión
    def set_password(self, user, pw, default=False):
        with self.lock:
            u = self.data["users"].get(user)
            if not u:
                raise UserError("usuario no encontrado")
            u["pw"] = hash_password(pw if default else _password(pw), self.iterations)
            u["default_pw"] = bool(default)
            self.save()

    def init_admin(self, pw=None):
        """Instalación: contraseña del administrador; sin ella queda admin/admin (y el panel avisa en cada inicio)."""
        if pw:
            self.set_password(ADMIN, pw)
        else:
            self.set_password(ADMIN, DEFAULT_PASSWORD, default=True)

    def create_user(self, name, pw, groups=()):
        with self.lock:
            _name(name, "nombre de usuario")
            if name in self.data["users"]:
                raise UserError("ese usuario ya existe")
            groups = self._check_groups(groups)
            self.data["users"][name] = {"pw": hash_password(_password(pw), self.iterations), "groups": groups,
                                        "default_pw": False, "disabled": False, "created": int(time.time())}
            self.save()

    def update_user(self, name, groups=None, disabled=None):
        with self.lock:
            u = self.data["users"].get(name)
            if not u:
                raise UserError("usuario no encontrado")
            if groups is not None:
                groups = self._check_groups(groups)
                if name == ADMIN and ADMIN_GROUP not in groups:
                    raise UserError("admin no puede salir del grupo de administradores")
                u["groups"] = groups
            if disabled is not None:
                if name == ADMIN and disabled:
                    raise UserError("admin no se puede desactivar")
                u["disabled"] = bool(disabled)
                if disabled:
                    self._purge(name)
            self.save()

    def delete_user(self, name):
        with self.lock:
            if name == ADMIN:
                raise UserError("admin no se puede borrar")
            if name not in self.data["users"]:
                raise UserError("usuario no encontrado")
            del self.data["users"][name]
            self.data["keys"] = {k: v for k, v in self.data["keys"].items() if v["user"] != name}
            self._purge(name)
            self.save()

    def _check_groups(self, groups):
        groups = list(dict.fromkeys(groups or []))
        for g in groups:
            if g not in self.data["groups"]:
                raise UserError(f"grupo desconocido: {g}")
        return groups

    def set_group(self, name, roles):
        with self.lock:
            _name(name, "nombre de grupo")
            roles = list(dict.fromkeys(roles or []))
            for r in roles:
                if r not in self.data["roles"]:
                    raise UserError(f"rol desconocido: {r}")
            if name == ADMIN_GROUP and ADMIN_ROLE not in roles:
                raise UserError("el grupo de administradores debe conservar el rol de administrador")
            self.data["groups"][name] = {"roles": roles}
            self.save()

    def delete_group(self, name):
        with self.lock:
            if name == ADMIN_GROUP:
                raise UserError("el grupo de administradores no se puede borrar")
            if name not in self.data["groups"]:
                raise UserError("grupo no encontrado")
            del self.data["groups"][name]
            for u in self.data["users"].values():
                u["groups"] = [g for g in u["groups"] if g != name]
            self.save()

    def set_role(self, name, perms):
        with self.lock:
            _name(name, "nombre de rol")
            perms = list(dict.fromkeys(perms or []))
            for p in perms:
                if p not in PERMS:
                    raise UserError(f"permiso desconocido: {p}")
            if name == ADMIN_ROLE and set(perms) != set(PERMS):
                raise UserError("el rol de administrador tiene siempre todos los permisos")
            self.data["roles"][name] = {"perms": perms}
            self.save()

    def delete_role(self, name):
        with self.lock:
            if name == ADMIN_ROLE:
                raise UserError("el rol de administrador no se puede borrar")
            if name not in self.data["roles"]:
                raise UserError("rol no encontrado")
            del self.data["roles"][name]
            for g in self.data["groups"].values():
                g["roles"] = [r for r in g["roles"] if r != name]
            self.save()

    def create_key(self, user, label, key=None):
        """Crea una API key para `user` y devuelve el texto (solo esta vez)."""
        with self.lock:
            if user not in self.data["users"]:
                raise UserError("usuario no encontrado")
            label = str(label or "").strip()[:64] or "clave"
            key = key or KEY_PREFIX + secrets.token_urlsafe(24)
            kid = secrets.token_hex(6)
            self.data["keys"][kid] = {"user": user, "name": label, "hash": key_hash(key), "prefix": key[:10],
                                      "created": int(time.time()), "last_used": 0}
            self.save()
            return kid, key

    def delete_key(self, kid, user=None):
        with self.lock:
            k = self.data["keys"].get(kid)
            if not k or (user and k["user"] != user):
                raise UserError("clave no encontrada")
            del self.data["keys"][kid]
            self.save()

    # ------------------------------------------------------------------ vistas (sin hashes)
    def view(self):
        with self.lock:
            return {"perms": list(PERMS), "perm_info": PERM_INFO,
                    "roles": {k: v["perms"] for k, v in self.data["roles"].items()},
                    "groups": {k: v["roles"] for k, v in self.data["groups"].items()},
                    "users": {k: {"groups": v["groups"], "disabled": v.get("disabled", False),
                                  "default_pw": v.get("default_pw", False), "created": v.get("created"),
                                  "perms": sorted(self.perms_of(k))} for k, v in self.data["users"].items()},
                    "keys": {k: {"user": v["user"], "name": v["name"], "prefix": v["prefix"], "created": v["created"],
                                 "last_used": v.get("last_used", 0)} for k, v in self.data["keys"].items()}}


# ---------------------------------------------------------------------- token local para la terminal
def local_token_path(home):
    return Path(home) / "local.token"


def write_local_token(home):
    """Token aleatorio que el orquestador crea al arrancar (permisos 600) para que `dllm status/load…` en este mismo
    equipo funcionen sin pedir usuario: solo se acepta desde 127.0.0.1 y equivale a admin."""
    tok = secrets.token_urlsafe(32)
    p = local_token_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def read_local_token(home=None):
    try:
        from . import config
        return local_token_path(home or config.home()).read_text().strip() or None
    except OSError:
        return None
