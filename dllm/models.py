# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Gestión de modelos del orquestador: catálogo local, descargas desde Hugging Face y Ollama, importación desde
una instalación local de Ollama, borrado y parámetros de generación por modelo.

Seguridad:
- Solo se contacta con las URLs base configuradas (Hugging Face y el registro de Ollama); el usuario aporta nombres,
  nunca URLs. Los nombres se validan y se codifican en la ruta.
- El token de Hugging Face (modelos con acceso restringido) solo se envía a huggingface.co: se elimina de la
  petición si una redirección lleva a otro host (CDN).
- Cada descarga se verifica: sha256 publicado (HF: lfs.oid; Ollama: el digest) y cabecera GGUF. Nada se da por
  bueno hasta que pasa la verificación; mientras tanto vive en un fichero .part.
- Se comprueba el espacio libre antes de empezar.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config, gguf

GiB = 1 << 30
UA = {"User-Agent": f"dllm/{__version__}"}
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
OLLAMA_MODEL = "application/vnd.ollama.image.model"
OLLAMA_PARAMS = "application/vnd.ollama.image.params"
OLLAMA_SYSTEM = "application/vnd.ollama.image.system"
OLLAMA_PROJECTOR = "application/vnd.ollama.image.projector"  # proyector multimodal (visión/audio)
MMPROJ_PREF = ("F16", "BF16", "Q8_0", "F32")  # calidad/tamaño recomendados para el proyector
FREE_MARGIN = GiB  # espacio que se deja libre además del fichero

RE_REPO = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[\w.-]{1,96}$")
RE_OLLAMA = re.compile(r"^(?:([a-z0-9][a-z0-9._-]{0,63})/)?([a-z0-9][a-z0-9._-]{0,127})(?::([\w][\w.-]{0,127}))?$")
RE_SPLIT = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$")
RE_QUANT = re.compile(r"(?i)(?:^|[-_.])((?:I?Q\d(?:_[A-Z0-9]+)*)|BF16|F16|F32)(?=[-_.]|\.gguf$)")


def hf_base():
    return os.environ.get("DLLM_HF_URL", "https://huggingface.co").rstrip("/")


def ollama_base():
    return os.environ.get("DLLM_OLLAMA_URL", "https://registry.ollama.ai").rstrip("/")


def ollama_local_dir():
    d = os.environ.get("OLLAMA_MODELS")
    return Path(d) if d else Path.home() / ".ollama" / "models"


class ModelError(Exception):
    pass


# ============================================================ HTTP
class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones, pero no reenvía Authorization a otro host (p. ej. la CDN de Hugging Face)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for h in list(new.headers):
                if h.lower() == "authorization":
                    del new.headers[h]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_StripAuthRedirect)


def _open(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        return _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (401, 403):
            hint = " (modelo con acceso restringido: acepta sus condiciones en su página y configura tu token de Hugging Face)"
        elif e.code == 404:
            hint = " (no existe: revisa el nombre)"
        raise ModelError(f"HTTP {e.code} en {urllib.parse.urlsplit(url).path}{hint}")
    except (urllib.error.URLError, OSError) as e:
        raise ModelError(f"no se pudo conectar con {urllib.parse.urlsplit(url).netloc}: {getattr(e, 'reason', e)}")


def _json(url, headers=None):
    with _open(url, headers) as r:
        return json.load(r)


# ============================================================ utilidades
def quant_of(name, meta=None):
    m = RE_QUANT.search(os.path.basename(name))
    return m.group(1).upper() if m else None


def is_mmproj(name):
    return "mmproj" in os.path.basename(name).lower()


def pick_mmproj(names):
    """Elige el proyector multimodal preferido entre varios (F16 > BF16 > Q8_0 > F32 > el primero)."""
    names = sorted(n for n in names if is_mmproj(n))
    if not names:
        return None
    for q in MMPROJ_PREF:
        for n in names:
            if quant_of(n) == q:
                return n
    return names[0]


def _safe_component(s):
    return re.sub(r"[^\w.-]", "-", s).strip(".-") or "modelo"


def _inside(root, p):
    root, p = Path(root).resolve(), Path(p).resolve()
    return root == p or root in p.parents


# ============================================================ Hugging Face
def hf_search(query, token=None, limit=25):
    q = (query or "").strip()
    if not q or len(q) > 100:
        raise ModelError("escribe qué buscar (máx. 100 caracteres)")
    params = urllib.parse.urlencode({"search": q, "filter": "gguf", "sort": "downloads", "direction": "-1",
                                     "limit": str(limit)})
    data = _json(f"{hf_base()}/api/models?{params}", _hf_auth(token))
    return [{"id": m.get("id"), "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
             "gated": bool(m.get("gated"))} for m in data if m.get("id")]


def _hf_auth(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _check_repo(repo):
    if not RE_REPO.match(repo or "") or ".." in repo:
        raise ModelError("repositorio no válido: usa el formato propietario/nombre")


def hf_files(repo, token=None):
    """Ficheros .gguf del repo agrupando los troceados (-0000N-of-0000M). Devuelve [{file, size, parts, quant}]."""
    _check_repo(repo)
    url = f"{hf_base()}/api/models/{urllib.parse.quote(repo)}/tree/main?recursive=true"
    entries = [e for e in _json(url, _hf_auth(token)) if e.get("type") == "file" and e.get("path", "").endswith(".gguf")]
    groups = {}
    for e in entries:
        path = e["path"]
        m = RE_SPLIT.match(path)
        key = m.group(1) + ".gguf" if m else path
        groups.setdefault(key, []).append(e)
    out = []
    for key, parts in sorted(groups.items()):
        parts.sort(key=lambda e: e["path"])
        out.append({"file": parts[0]["path"], "size": sum(int((p.get("lfs") or {}).get("size") or p.get("size") or 0)
                                                        for p in parts),
                    "parts": len(parts), "quant": quant_of(key),
                    "mmproj": "mmproj" in key.lower()})  # proyectores de visión: no son un modelo de texto
    return out


def _hf_plan(repo, file, token):
    """Lista de descargas [(url, nombre_destino, size, sha256)] para un fichero (todas sus partes si está troceado)."""
    _check_repo(repo)
    if not file or ".." in file.split("/") or file.startswith("/") or not file.endswith(".gguf"):
        raise ModelError("fichero no válido: debe ser un .gguf del repositorio")
    url = f"{hf_base()}/api/models/{urllib.parse.quote(repo)}/tree/main?recursive=true"
    entries = {e["path"]: e for e in _json(url, _hf_auth(token)) if e.get("type") == "file"}
    m = RE_SPLIT.match(file)
    if m:
        wanted = sorted(p for p in entries if RE_SPLIT.match(p) and RE_SPLIT.match(p).group(1) == m.group(1))
    else:
        wanted = [file]
    if not wanted or any(w not in entries for w in wanted):
        raise ModelError(f"{file} no está en {repo}")
    if not is_mmproj(file):
        # modelo multimodal: su proyector (visión/audio) se descarga con él automáticamente
        proj = pick_mmproj([p for p in entries if p.endswith(".gguf") and os.path.dirname(p) == os.path.dirname(file)])
        if proj:
            wanted.append(proj)
    jobs = []
    for w in wanted:
        e = entries[w]
        lfs = e.get("lfs") or {}
        sha = lfs.get("oid") if re.fullmatch(r"[0-9a-f]{64}", str(lfs.get("oid", ""))) else None
        jobs.append((f"{hf_base()}/{urllib.parse.quote(repo)}/resolve/main/{urllib.parse.quote(w)}",
                     os.path.basename(w), int(lfs.get("size") or e.get("size") or 0), sha))
    return jobs


# ============================================================ Ollama
def parse_ollama(name):
    m = RE_OLLAMA.match((name or "").strip().lower())
    if not m:
        raise ModelError("nombre de Ollama no válido: usa nombre o nombre:etiqueta (p. ej. qwen2.5:7b)")
    return m.group(1) or "library", m.group(2), m.group(3) or "latest"


def ollama_manifest(name):
    ns, model, tag = parse_ollama(name)
    url = f"{ollama_base()}/v2/{ns}/{urllib.parse.quote(model)}/manifests/{urllib.parse.quote(tag)}"
    return (ns, model, tag), _json(url, {"Accept": MANIFEST_ACCEPT})


def _layer(manifest, media):
    return next((l for l in manifest.get("layers", []) if l.get("mediaType") == media), None)


def _digest_hex(d):
    m = re.fullmatch(r"sha256:([0-9a-f]{64})", d or "")
    if not m:
        raise ModelError("digest de capa no válido en el manifiesto")
    return m.group(1)


def ollama_params_to_settings(params, system=None):
    """Convierte los parámetros por defecto de un modelo de Ollama a los de DLLM."""
    mp = {"temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "min_p": "min_p",
          "repeat_penalty": "repeat_penalty", "num_predict": "max_tokens", "num_ctx": "ctx", "stop": "stop"}
    out = {}
    for k, v in (params or {}).items():
        if k in mp:
            out[mp[k]] = v if k != "stop" else ([v] if isinstance(v, str) else list(v))
    if system:
        out["system_prompt"] = system
    try:
        return validate_settings(out)
    except ModelError:
        return {}


def ollama_local_list():
    """Modelos de una instalación local de Ollama: [{name, size, blob}]."""
    root = ollama_local_dir()
    mroot = root / "manifests"
    out = []
    if not mroot.is_dir():
        return out
    for f in mroot.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(mroot).parts  # registro/ns/modelo/tag
            man = json.loads(f.read_text())
            layer = _layer(man, OLLAMA_MODEL)
            if not layer or len(rel) < 3:
                continue
            blob = root / "blobs" / layer["digest"].replace(":", "-")
            if not blob.is_file():
                continue
            ns, model, tag = rel[-3], rel[-2], rel[-1]
            name = f"{model}:{tag}" if ns == "library" else f"{ns}/{model}:{tag}"
            out.append({"name": name, "size": blob.stat().st_size, "blob": str(blob), "manifest": str(f)})
        except (ValueError, OSError, KeyError):
            continue
    return sorted(out, key=lambda x: x["name"])


# ============================================================ parámetros de generación
SETTING_RULES = {
    # generación (se aplican a cada petición sin pisar lo que envía el cliente)
    "temperature": (float, 0.0, 5.0), "top_p": (float, 0.0, 1.0), "top_k": (int, 0, 1000),
    "min_p": (float, 0.0, 1.0), "typical_p": (float, 0.0, 1.0), "repeat_penalty": (float, 0.5, 2.5),
    "presence_penalty": (float, -2.0, 2.0), "frequency_penalty": (float, -2.0, 2.0),
    "max_tokens": (int, 1, 1048576), "seed": (int, -1, 2 ** 31 - 1),
    # carga (se aplican al lanzar llama-server)
    "ctx": (int, 256, 4194304), "batch_size": (int, 32, 65536), "ubatch_size": (int, 16, 65536),
    "parallel": (int, 1, 64), "rope_freq_base": (float, 1.0, 1e8), "rope_freq_scale": (float, 0.001, 100.0),
    # reutilizar la caché KV entre peticiones con prefijo común (desplazando trozos de al menos N tokens)
    "cache_reuse": (int, 0, 65536),
    # decodificación especulativa: un modelo pequeño propone tokens y el grande los valida
    "draft_max": (int, 1, 64), "draft_min": (int, 0, 64),
}
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "typical_p", "repeat_penalty", "presence_penalty",
                 "frequency_penalty", "max_tokens", "seed", "stop")
KV_TYPES = ("f16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl", "bf16", "f32")
ENUMS = {"cache_type_k": KV_TYPES, "cache_type_v": KV_TYPES, "flash_attn": ("auto", "on", "off"),
         "rope_scaling": ("none", "linear", "yarn"), "datetime": ("datetime", "date", "off"),
         "placement": ("auto", "split", "node"), "engine": ("llamacpp",),
         "device_policy": ("auto", "gpu_strict", "cpu_only", "gpu_with_cpu_fallback"),
         "tools": ("auto", "on", "off"), "local_gpu": ("auto", "on", "off")}
BOOLS = ("embeddings",)
# ajuste → opción de llama-server (comprobadas en su documentación)
LOAD_FLAGS = {"cache_type_k": "-ctk", "cache_type_v": "-ctv", "flash_attn": "-fa", "batch_size": "-b",
              "ubatch_size": "-ub", "parallel": "-np", "rope_freq_base": "--rope-freq-base",
              "rope_freq_scale": "--rope-freq-scale", "rope_scaling": "--rope-scaling",
              "cache_reuse": "--cache-reuse", "draft_max": "--draft-max", "draft_min": "--draft-min"}


def load_args(settings):
    """Opciones de llama-server derivadas de los ajustes de carga del modelo."""
    out = []
    for k, flag in LOAD_FLAGS.items():
        v = (settings or {}).get(k)
        if v is not None:
            out += [flag, str(v)]
    return out


def validate_settings(s):
    out = _validate_settings(s)
    # combinaciones que llama.cpp no admite
    if out.get("cache_type_v", "f16") not in ("f16", "bf16", "f32") and out.get("flash_attn") == "off":
        raise ModelError("la caché V cuantizada requiere flash attention (on o auto)")
    if out.get("ubatch_size") and out.get("batch_size") and out["ubatch_size"] > out["batch_size"]:
        raise ModelError("el lote físico (ubatch) no puede ser mayor que el lógico (batch)")
    if out.get("draft_min") is not None and out.get("draft_max") is not None and out["draft_min"] > out["draft_max"]:
        raise ModelError("draft_min no puede ser mayor que draft_max")
    return out


def _validate_settings(s):
    out = {}
    for k, v in (s or {}).items():
        if v is None or (v == "" and k != "mmproj"):  # mmproj "" = desactivado a propósito
            continue
        if k in SETTING_RULES:
            typ, lo, hi = SETTING_RULES[k]
            try:
                f = float(v)
            except (TypeError, ValueError):
                raise ModelError(f"'{k}' debe ser un número")
            if typ is int:
                if f != int(f):
                    raise ModelError(f"'{k}' debe ser un entero")
                f = int(f)
            if not lo <= f <= hi:
                raise ModelError(f"'{k}' debe estar entre {lo} y {hi}")
            out[k] = f
        elif k in ENUMS:
            if v not in ENUMS[k]:
                raise ModelError(f"'{k}' debe ser uno de: {', '.join(ENUMS[k])}")
            out[k] = v
        elif k in BOOLS:
            if not isinstance(v, bool):
                raise ModelError(f"'{k}' debe ser verdadero o falso")
            out[k] = v
        elif k == "mmproj":
            if not isinstance(v, str) or (v and (not v.endswith(".gguf") or ".." in v.split("/") or v.startswith("/"))):
                raise ModelError("proyector multimodal no válido")
            out[k] = v
        elif k == "draft_model":
            if not isinstance(v, str) or not v.endswith(".gguf") or ".." in v.split("/") or v.startswith("/") \
                    or "\\" in v:
                raise ModelError("modelo borrador no válido (un .gguf de la carpeta de modelos)")
            out[k] = v
        elif k == "system_prompt":
            if not isinstance(v, str) or len(v) > 20000:
                raise ModelError("el prompt de sistema debe ser texto (máx. 20000 caracteres)")
            out[k] = v
        elif k == "stop":
            if isinstance(v, str):
                v = [x for x in (t.strip() for t in v.split(",")) if x]
            if not isinstance(v, list) or len(v) > 8 or not all(isinstance(x, str) and 0 < len(x) <= 64 for x in v):
                raise ModelError("'stop' admite hasta 8 secuencias de texto de 1 a 64 caracteres")
            out[k] = v
        else:
            raise ModelError(f"parámetro desconocido: {k}")
    return out


_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")


_DAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December")


def _tz(t):
    off = getattr(t, "tm_gmtoff", None)
    off = -time.timezone if off is None else off
    sign, off = ("+" if off >= 0 else "-"), abs(int(off))
    zone = getattr(t, "tm_zone", None) or time.tzname[0]
    zone = zone if zone and zone.isascii() and len(zone) <= 6 else ""  # Windows da nombres largos y traducidos
    utc = f"UTC{sign}{off // 3600:02d}:{off % 3600 // 60:02d}" if off else "UTC"
    return utc if not zone or zone in ("UTC", "GMT") else f"{zone}, {utc}"


def _en():
    from . import i18n
    return i18n.lang() == "en"


def time_text(now=None):
    t = now or time.localtime()
    return (f"server time: {t.tm_hour:02d}:{t.tm_min:02d} ({_tz(t)})" if _en() else
            f"hora del servidor: {t.tm_hour:02d}:{t.tm_min:02d} ({_tz(t)})")


def now_text(mode="datetime", now=None):
    """«Fecha y hora actuales (reloj del servidor): domingo, 15 de noviembre de 2026, 10:32 (CET, UTC+01:00).»
    Independiente del idioma del sistema. Los modelos no saben la fecha: sin esto suelen inventarla."""
    t = now or time.localtime()
    if _en():
        s = f"{_DAYS_EN[t.tm_wday]}, {_MONTHS_EN[t.tm_mon - 1]} {t.tm_mday}, {t.tm_year}"
        if mode == "date":
            return f"Current date (server clock): {s}."
        return f"Current date and time (server clock): {s}, {t.tm_hour:02d}:{t.tm_min:02d} ({_tz(t)})."
    s = f"{_DIAS[t.tm_wday]}, {t.tm_mday} de {_MESES[t.tm_mon - 1]} de {t.tm_year}"
    if mode == "date":
        return f"Fecha actual (reloj del servidor): {s}."
    return f"Fecha y hora actuales (reloj del servidor): {s}, {t.tm_hour:02d}:{t.tm_min:02d} ({_tz(t)})."


def _append_time(msgs, mode, now):
    if mode != "datetime":
        return
    last = next((m for m in reversed(msgs) if isinstance(m, dict) and m.get("role") == "user"), None)
    if last is None:
        return
    note = time_text(now)
    c = last.get("content")
    if isinstance(c, str):
        last["content"] = f"{c}\n\n({note})" if c else f"({note})"
    elif isinstance(c, list):
        last["content"] = c + [{"type": "text", "text": f"({note})"}]


def apply_settings(path, body, settings, now=None):
    """Completa una petición OpenAI con los parámetros por defecto del modelo, sin pisar lo que envía el cliente,
    y le da al modelo la fecha y hora del servidor (ajuste «datetime», activado por defecto)."""
    if not isinstance(body, dict):
        return body
    settings = settings or {}
    for k in SAMPLING_KEYS:
        if k in settings and body.get(k) is None:
            body[k] = settings[k]
    if not (path.endswith("/chat/completions") and isinstance(body.get("messages"), list)):
        return body
    body.setdefault("cache_prompt", True)  # reutiliza la caché KV del prefijo común entre turnos
    sp = settings.get("system_prompt")
    mode = settings.get("datetime", "datetime")
    # en el prompt de sistema va solo la fecha (cambia una vez al día); la hora va al final del último mensaje del
    # usuario: así el principio de la conversación no cambia cada minuto y llama.cpp reutiliza la caché del prompt
    extra = now_text("date", now) if mode != "off" else None
    _append_time(body["messages"], mode, now)
    msgs = body["messages"]
    first = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "system"), None)
    if first is None:
        text = "\n\n".join(x for x in (sp, extra) if x)
        if text:
            body["messages"] = [{"role": "system", "content": text}] + msgs
    elif extra:  # el cliente trae su prompt de sistema: se respeta y se le añade la fecha
        c = first.get("content")
        if isinstance(c, str):
            first["content"] = (c + "\n\n" + extra) if c else extra
        elif isinstance(c, list):
            first["content"] = c + [{"type": "text", "text": extra}]
    return body


# ============================================================ descargas
class Job:
    _n = 0

    def __init__(self, label, source, steps, target_dir, settings=None):
        Job._n += 1
        self.id = f"d{Job._n}"
        self.label, self.source, self.steps, self.dir = label, source, steps, Path(target_dir)
        self.settings = settings  # parámetros importados (Ollama) que se guardan al terminar
        self.mmproj = next((st["name"] for st in steps if is_mmproj(st["name"])), None)
        self.total = sum(s["size"] for s in steps)
        self.done = 0
        self.status, self.error, self.result = "en cola", "", None
        self.t0 = self.t_end = None
        self.cancel = threading.Event()

    def view(self):
        el = (self.t_end or time.time()) - self.t0 if self.t0 else 0
        speed = self.done / el if el > 0 else 0
        return {"id": self.id, "label": self.label, "source": self.source, "status": self.status,
                "error": self.error, "total": self.total, "done": self.done, "result": self.result,
                "pct": round(self.done * 100 / self.total, 1) if self.total else None,
                "speed": int(speed), "eta": int((self.total - self.done) / speed) if speed and self.total else None}


class Manager:
    def __init__(self, models_dir, settings_file=None, secrets_file=None):
        # ruta real: con enlaces simbólicos (macOS /var → /private/var) o nombres cortos de Windows, las
        # comparaciones de rutas fallarían si no se resuelve
        self.root = Path(models_dir).expanduser().resolve()
        self.settings_file = Path(settings_file or (config.home() / "models.json"))
        self.secrets_file = Path(secrets_file or (config.home() / "secrets.json"))
        self.jobs, self.lock = [], threading.Lock()
        self.queue = threading.Condition(self.lock)
        self._meta_cache = {}
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------------------------------------------------- secretos (token HF)
    def hf_token(self):
        try:
            t = json.loads(self.secrets_file.read_text()).get("hf_token")
        except (OSError, ValueError):
            t = None
        return t or os.environ.get("HF_TOKEN") or None

    def set_hf_token(self, token):
        token = (token or "").strip()
        if token and not re.fullmatch(r"hf_[A-Za-z0-9]{20,}", token):
            raise ModelError("el token de Hugging Face empieza por hf_ (créalo en huggingface.co/settings/tokens)")
        self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.secrets_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"hf_token": token or None}))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.secrets_file)
        return bool(token)

    # ---------------------------------------------------------- parámetros por modelo
    def all_settings(self):
        try:
            return json.loads(self.settings_file.read_text())
        except (OSError, ValueError):
            return {}

    def settings(self, name):
        return self.all_settings().get(name, {})

    def set_settings(self, name, s):
        self.resolve(name)
        clean = validate_settings(s)
        if clean.get("mmproj"):
            self.resolve(clean["mmproj"])
        allset = self.all_settings()
        if clean:
            allset[name] = clean
        else:
            allset.pop(name, None)
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(allset, indent=2, ensure_ascii=False))
        return clean

    # ---------------------------------------------------------- catálogo
    def resolve(self, name):
        p = (self.root / (name or "")).resolve()
        if not _inside(self.root, p) or p.suffix != ".gguf" or not p.is_file():
            raise ModelError(f"modelo no válido o fuera de {self.root}: {name}")
        return p

    def _meta(self, p):
        key = (str(p), p.stat().st_mtime)
        if key not in self._meta_cache:
            try:
                m = gguf.read_model(str(p))
                self._meta_cache[key] = {"arch": m["arch"], "n_layers": m["n_layers"], "ctx_train": m["ctx_train"],
                                         "general_name": m["name"], "size": m["size"], "parts": m["parts"],
                                         "chat_template": m["chat_template"], "tool_template": m["tool_template"]}
            except Exception as e:
                self._meta_cache[key] = {"error": f"no se pudo leer: {e}", "size": p.stat().st_size, "parts": 1}
        return self._meta_cache[key]

    def mmproj_candidates(self, name):
        d = self.resolve(name).parent
        return [str(f.relative_to(self.root)).replace(os.sep, "/") for f in sorted(d.glob("*.gguf")) if is_mmproj(f.name)]

    def mmproj_for(self, name):
        """Ruta del proyector a usar: el elegido en los parámetros ("" = ninguno) o, si no se eligió, el que haya en
        la misma carpeta (detección automática)."""
        s = self.settings(name)
        if "mmproj" in s:
            return str(self.resolve(s["mmproj"])) if s["mmproj"] else None
        auto = pick_mmproj(self.mmproj_candidates(name))
        return str(self.resolve(auto)) if auto else None

    def list(self):
        out, sets = [], self.all_settings()
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.rglob("*.gguf")):
            rel = p.relative_to(self.root)
            if len(rel.parts) > 4 or is_mmproj(p.name):
                continue
            m = RE_SPLIT.match(p.name)
            if m and m.group(2) != "00001":
                continue  # solo la primera parte de los troceados
            name = str(rel).replace(os.sep, "/")
            src = rel.parts[0] if rel.parts[0] in ("hf", "ollama") and len(rel.parts) > 1 else "local"
            item = dict(self._meta(p), name=name, source=src, quant=quant_of(p.name), settings=sets.get(name, {}))
            try:
                mp = self.mmproj_for(name)
                item["mmproj"] = str(Path(mp).relative_to(self.root)).replace(os.sep, "/") if mp else None
                item["mmproj_candidates"] = self.mmproj_candidates(name)
            except ModelError:
                item["mmproj"], item["mmproj_candidates"] = None, []
            item["vision"] = bool(item.get("mmproj"))
            out.append(item)
        return out

    def list_one(self, name):
        """Metadatos de un solo modelo (sin recorrer toda la carpeta)."""
        try:
            return dict(self._meta(self.resolve(name)))
        except ModelError:
            return {}

    def delete(self, name):
        p = self.resolve(name)
        m = RE_SPLIT.match(p.name)
        files = [p.parent / f"{m.group(1)}-{i:05d}-of-{m.group(3)}.gguf" for i in range(1, int(m.group(3)) + 1)] \
            if m else [p]
        for f in files:
            if _inside(self.root, f) and f.is_file():
                f.unlink()
        try:  # carpeta vacía de hf/<repo>
            if p.parent != self.root and not any(p.parent.iterdir()):
                p.parent.rmdir()
        except OSError:
            pass
        allset = self.all_settings()
        if allset.pop(name, None) is not None:
            self.settings_file.write_text(json.dumps(allset, indent=2, ensure_ascii=False))

    # ---------------------------------------------------------- crear descargas
    def add_hf(self, repo, file):
        token = self.hf_token()
        steps = [{"url": u, "name": n, "size": s, "sha256": h, "auth": True} for u, n, s, h in _hf_plan(repo, file, token)]
        d = self.root / "hf" / _safe_component(repo.replace("/", "__"))
        extra = " + proyector multimodal" if any(is_mmproj(st["name"]) for st in steps[1:]) else ""
        return self._enqueue(Job(f"{repo} · {os.path.basename(file)}{extra}", "hf", steps, d))

    def add_ollama(self, name):
        (ns, model, tag), man = ollama_manifest(name)
        layer = _layer(man, OLLAMA_MODEL)
        if not layer:
            raise ModelError("ese modelo de Ollama no tiene una capa de modelo GGUF")
        blob = lambda l: f"{ollama_base()}/v2/{ns}/{urllib.parse.quote(model)}/blobs/{l['digest']}"
        params, system = None, None
        for media in (OLLAMA_PARAMS, OLLAMA_SYSTEM):
            l = _layer(man, media)
            if l and int(l.get("size") or 0) < 1 << 20:
                try:
                    with _open(blob(l)) as r:
                        data = r.read(1 << 20)
                    if media == OLLAMA_PARAMS:
                        params = json.loads(data)
                    else:
                        system = data.decode("utf-8", "replace")
                except (ModelError, ValueError):
                    pass
        fname = _safe_component(f"{model}-{tag}" if ns == "library" else f"{ns}-{model}-{tag}") + ".gguf"
        steps = [{"url": blob(layer), "name": fname, "size": int(layer.get("size") or 0),
                  "sha256": _digest_hex(layer["digest"]), "auth": False}]
        proj = _layer(man, OLLAMA_PROJECTOR)
        if proj:
            steps.append({"url": blob(proj), "name": fname[:-5] + "-mmproj.gguf", "size": int(proj.get("size") or 0),
                          "sha256": _digest_hex(proj["digest"]), "auth": False})
        return self._enqueue(Job(f"ollama {model}:{tag}" + (" + proyector multimodal" if proj else ""), "ollama",
                                 steps, self.root / "ollama",
                                 ollama_params_to_settings(params, system)))

    def add_ollama_local(self, name):
        item = next((m for m in ollama_local_list() if m["name"] == name), None)
        if not item:
            raise ModelError(f"{name} no está en la instalación local de Ollama ({ollama_local_dir()})")
        settings = {}
        try:
            man = json.loads(Path(item["manifest"]).read_text())
            root = ollama_local_dir() / "blobs"
            rd = lambda l: (root / l["digest"].replace(":", "-")).read_bytes()[:1 << 20]
            pl, sl = _layer(man, OLLAMA_PARAMS), _layer(man, OLLAMA_SYSTEM)
            settings = ollama_params_to_settings(json.loads(rd(pl)) if pl else None,
                                                 rd(sl).decode("utf-8", "replace") if sl else None)
        except (OSError, ValueError, KeyError):
            pass
        fname = _safe_component(name.replace("/", "-").replace(":", "-")) + ".gguf"
        steps = [{"url": None, "local": item["blob"], "name": fname, "size": item["size"],
                  "sha256": Path(item["blob"]).name.split("-", 1)[-1], "auth": False}]
        try:
            proj = _layer(json.loads(Path(item["manifest"]).read_text()), OLLAMA_PROJECTOR)
            pb = ollama_local_dir() / "blobs" / proj["digest"].replace(":", "-") if proj else None
            if pb and pb.is_file():
                steps.append({"url": None, "local": str(pb), "name": fname[:-5] + "-mmproj.gguf",
                              "size": pb.stat().st_size, "sha256": proj["digest"].split(":", 1)[1], "auth": False})
        except (OSError, ValueError, KeyError):
            pass
        return self._enqueue(Job(f"ollama local {name}", "ollama-local", steps, self.root / "ollama", settings))

    def _enqueue(self, job):
        with self.queue:
            for j in self.jobs:
                if j.status in ("en cola", "descargando") and j.label == job.label:
                    raise ModelError("esa descarga ya está en curso")
            self.jobs.append(job)
            self.jobs = self.jobs[-50:]
            self.queue.notify()
        return job.view()

    def cancel(self, jid):
        for j in self.jobs:
            if j.id == jid and j.status in ("en cola", "descargando"):
                j.cancel.set()
                if j.status == "en cola":
                    j.status = "cancelada"
                return True
        raise ModelError("descarga no encontrada o ya terminada")

    def views(self):
        return [j.view() for j in reversed(self.jobs)]

    def clear(self, jid=None):
        """Borra del historial las descargas terminadas (completadas, canceladas o con error): una (`jid`) o todas.
        No toca ficheros (un .part de una descarga cancelada sigue sirviendo para reanudar) ni las activas."""
        with self.lock:
            fin = lambda j: j.status not in ("en cola", "descargando")
            if jid is not None:
                j = next((x for x in self.jobs if x.id == jid), None)
                if j is None:
                    raise ModelError("descarga no encontrada")
                if not fin(j):
                    raise ModelError("esa descarga sigue activa: cancélala antes de borrarla")
            before = len(self.jobs)
            self.jobs = [j for j in self.jobs if not fin(j) or (jid is not None and j.id != jid)]
            return before - len(self.jobs)

    # ---------------------------------------------------------- ejecutar
    def _worker(self):
        while True:
            with self.queue:
                job = next((j for j in self.jobs if j.status == "en cola"), None)
                while job is None:
                    self.queue.wait()
                    job = next((j for j in self.jobs if j.status == "en cola"), None)
                job.status, job.t0 = "descargando", time.time()
            try:
                self._run(job)
                job.status = "completada"
                if job.result and (job.settings or job.mmproj):
                    cur = self.settings(job.result)
                    new = dict(job.settings or {}, **cur)  # lo que ya configuró el usuario manda
                    if job.mmproj and "mmproj" not in cur:
                        new["mmproj"] = str((job.dir / job.mmproj).relative_to(self.root)).replace(os.sep, "/")
                    if new != cur:
                        try:
                            self.set_settings(job.result, new)
                        except ModelError:
                            pass
            except _Cancelled:
                job.status = "cancelada"
            except Exception as e:
                job.status, job.error = "error", str(e)
            job.t_end = time.time()

    def _run(self, job):
        job.dir.mkdir(parents=True, exist_ok=True)
        if not _inside(self.root, job.dir):
            raise ModelError("destino fuera de la carpeta de modelos")
        pending = 0
        for s in job.steps:
            dest = job.dir / s["name"]
            if not dest.exists():
                part = dest.with_name(dest.name + ".part")
                pending += s["size"] - (part.stat().st_size if part.exists() else 0)
        free = shutil.disk_usage(job.dir).free
        if pending + FREE_MARGIN > free:
            raise ModelError(f"no hay espacio: hacen falta {pending / GiB:.1f} GiB (+1 GiB de margen) y quedan "
                             f"{free / GiB:.1f} GiB en {job.dir}")
        token = self.hf_token()
        base_done = 0
        for s in job.steps:
            dest = job.dir / s["name"]
            if not (_inside(self.root, dest)):
                raise ModelError("nombre de destino no válido")
            if dest.exists():
                base_done += dest.stat().st_size
                job.done = base_done
                continue
            if s.get("local"):
                self._copy_local(job, s, dest, base_done)
            else:
                self._fetch(job, s, dest, base_done, _hf_auth(token) if s["auth"] else {})
            base_done += dest.stat().st_size
        first = job.dir / job.steps[0]["name"]
        job.result = str(first.relative_to(self.root)).replace(os.sep, "/")

    def _verify_and_commit(self, part, dest, h, s):
        if s["sha256"] and h.hexdigest() != s["sha256"]:
            part.unlink(missing_ok=True)
            raise ModelError(f"{s['name']}: el sha256 no coincide con el publicado; descarga descartada")
        with open(part, "rb") as f:
            magic = f.read(4)
        if magic != b"GGUF":  # (fuera del with: Windows no deja borrar un fichero abierto)
            part.unlink(missing_ok=True)
            raise ModelError(f"{s['name']}: no es un fichero GGUF; descartado")
        os.replace(part, dest)

    def _fetch(self, job, s, dest, base_done, auth):
        part = dest.with_name(dest.name + ".part")
        h, have = hashlib.sha256(), 0
        if part.exists():  # reanudar: el hash debe incluir lo ya descargado
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                    have += len(chunk)
        headers = dict(auth)
        if have:
            headers["Range"] = f"bytes={have}-"
        r = _open(s["url"], headers, timeout=60)
        with r:
            if have and r.status != 206:  # el servidor no admite reanudar: empezar de cero
                h, have = hashlib.sha256(), 0
                mode = "wb"
            else:
                mode = "ab" if have else "wb"
            with open(part, mode) as f:
                job.done = base_done + have
                while True:
                    if job.cancel.is_set():
                        raise _Cancelled()  # el .part se conserva para reanudar después
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
                    job.done = base_done + have
        if s["size"] and have != s["size"]:
            raise ModelError(f"{s['name']}: descarga incompleta ({have} de {s['size']} bytes); vuelve a intentarlo "
                             "para reanudar")
        self._verify_and_commit(part, dest, h, s)

    def _copy_local(self, job, s, dest, base_done):
        src = Path(s["local"])
        try:
            os.link(src, dest)  # mismo disco: instantáneo y sin ocupar más espacio (el blob ya lo verificó Ollama)
            with open(dest, "rb") as f:
                ok = f.read(4) == b"GGUF"
            if not ok:
                dest.unlink()
                raise ModelError(f"{s['name']}: el blob de Ollama no es un GGUF compatible con llama.cpp")
            job.done = base_done + s["size"]
            return
        except OSError:
            pass
        part = dest.with_name(dest.name + ".part")
        h, have = hashlib.sha256(), 0
        with open(src, "rb") as fi, open(part, "wb") as fo:
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                if job.cancel.is_set():
                    part.unlink(missing_ok=True)
                    raise _Cancelled()
                fo.write(chunk)
                h.update(chunk)
                have += len(chunk)
                job.done = base_done + have
        self._verify_and_commit(part, dest, h, s)


class _Cancelled(Exception):
    pass
