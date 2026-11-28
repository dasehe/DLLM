# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Idiomas de las interfaces. Las páginas se escriben en español (idioma fuente) y se muestran en inglés por defecto:
el navegador traduce el DOM con el diccionario `en.json` (español → inglés); el usuario puede elegir Español (España).
Cada texto visible nuevo debe tener su entrada en `en.json` (lo comprueba tests/test_i18n.py; `scripts/i18n_extract.py`
lista los que faltan)."""
import json
import os
import pkgutil
import re
import sys

LANGS = {"en": "English", "es": "Español (España)"}
DEFAULT = "en"
_CACHE = {}


def dictionary(lang="en"):
    if lang not in _CACHE:
        raw = pkgutil.get_data("dllm", f"i18n/{lang}.json")
        _CACHE[lang] = json.loads(raw.decode("utf-8")) if raw else {}
    return _CACHE[lang]


def inject(html):
    """Inserta el diccionario y el traductor en una página (marcador <!--I18N--> en su <head>)."""
    js = pkgutil.get_data("dllm", "i18n/runtime.js").decode("utf-8")
    data = json.dumps(dictionary("en"), ensure_ascii=False).replace("</", "<\\/")
    pats = json.dumps(patterns("en"), ensure_ascii=False).replace("</", "<\\/")
    tpls = json.dumps(templates("en"), ensure_ascii=False).replace("</", "<\\/")
    return html.replace("<!--I18N-->", f"<script>window.__I18N={data};window.__I18N_RE={pats};"
                                       f"window.__I18N_TPL={tpls};</script>"
                                       f"<script>{js}</script>", 1)


def patterns(lang="en"):
    """Expresiones regulares para textos con valores dentro (p. ej. «de 6.0 GiB»): [[patrón, sustitución], ...]."""
    raw = pkgutil.get_data("dllm", f"i18n/{lang}.re.json")
    return json.loads(raw.decode("utf-8")) if raw else []


def templates(lang="en"):
    """Mensajes con valores dentro como plantillas completas: {"nodo {0} de nuevo en línea": "node {0} back online"}.
    Se prueban antes que los trozos sueltos, así una frase con valores nunca queda a medio traducir; los valores se
    traducen también si son texto conocido."""
    if ("tpl", lang) not in _CACHE:
        raw = pkgutil.get_data("dllm", f"i18n/{lang}.tpl.json")
        _CACHE[("tpl", lang)] = json.loads(raw.decode("utf-8")) if raw else {}
    return _CACHE[("tpl", lang)]


_TPL = []


def _compiled_templates():
    if not _TPL:
        for src, dst in templates("en").items():
            parts = re.split(r"\{(\d+)\}", src)
            rx, order = "", []
            for i, part in enumerate(parts):
                if i % 2:
                    order.append(int(part))
                    rx += "(.+?)"
                else:
                    rx += re.escape(part)
            _TPL.append((re.compile("^" + rx + "$", re.S), dst, order))
    return _TPL


def _fill(m, dst, order, depth):
    vals = {n: m.group(i + 1) for i, n in enumerate(order)}
    return re.sub(r"\{(\d+)\}", lambda x: _tr_value(vals.get(int(x.group(1)), x.group(0)), depth), dst)


def _tr_value(v, depth):
    return tr(v, "en", depth + 1) if depth < 2 and re.search(r"[a-záéíóúñ]{2}", v, re.I) else v


def lang():
    """Idioma de la terminal y de los mensajes del servidor: DLLM_LANG (en|es), si no `lang` de config.json, si no inglés."""
    v = (os.environ.get("DLLM_LANG") or "").lower()[:2]
    if v in LANGS:
        return v
    try:
        from . import config
        v = ((config.load() or {}).get("lang") or "").lower()[:2]
    except Exception:
        v = ""
    return v if v in LANGS else DEFAULT


_KEYS = []
_RES = []


def tr(text, to=None, _depth=0):
    """Traduce un mensaje (español fuente) al inglés con el mismo algoritmo que el navegador: texto exacto, trozos conocidos
    dentro del texto (mensajes con valores) y expresiones. Con to='es' (o idioma es) lo devuelve tal cual."""
    if (to or lang()) != "en" or not isinstance(text, str) or not text.strip():
        return text
    d = dictionary("en")
    t = text.strip()
    if t in d:
        return text.replace(t, d[t])
    c = " ".join(t.split())
    if c in d:
        return text.replace(t, d[c])
    if not re.search(r"[a-záéíóúñ]{2}", t, re.I):
        return text
    for rx, dst, order in _compiled_templates():
        m = rx.match(t) or (rx.match(c) if c != t else None)
        if m:
            return text.replace(t, _fill(m, dst, order, _depth))
    if not _KEYS:
        _KEYS.extend(sorted((k for k in d if len(k) >= 5 and " " in k.strip()), key=len, reverse=True))
        _RES.extend((re.compile(p), r.replace("$1", r"\1").replace("$2", r"\2")) for p, r in patterns("en"))
    out = c if "\n" in t else t
    base = out
    for k in _KEYS:
        if k in out:
            out = out.replace(k, d[k])
    for rx, rep in _RES:
        out = rx.sub(rep, out)
    return text if out == base else text.replace(t, out)


class _TrStream:
    """Envuelve stdout/stderr para que la terminal salga en el idioma elegido sin tocar cada print()."""

    def __init__(self, raw):
        self._raw = raw

    def write(self, s):
        if s and s.strip() and not s.lstrip().startswith(("{", "[")) and "DLLM1-" not in s:
            s = tr(s)
        return self._raw.write(s)

    def __getattr__(self, name):
        return getattr(self._raw, name)


def install_terminal():
    if lang() == "en" and not isinstance(sys.stdout, _TrStream):
        import argparse  # la ayuda se parte en líneas: se traduce antes de maquetarla
        gh, ft = argparse.HelpFormatter._get_help_string, argparse.HelpFormatter._format_text
        argparse.HelpFormatter._get_help_string = lambda self, a: tr(gh(self, a))
        argparse.HelpFormatter._format_text = lambda self, t: ft(self, tr(t))
        if sys.stdout is not None:
            sys.stdout = _TrStream(sys.stdout)
        if sys.stderr is not None:
            sys.stderr = _TrStream(sys.stderr)


def request_lang(headers, query=""):
    """Idioma pedido por un cliente HTTP: cabecera X-DLLM-Lang (la manda el panel), ?lang= o el del servidor."""
    v = ((headers.get("X-DLLM-Lang") if headers else "") or "").lower()[:2]
    if v not in LANGS and "lang=" in (query or ""):
        v = query.split("lang=", 1)[1][:2].lower()
    return v if v in LANGS else lang()


def tr_error(obj, to):
    """Traduce el campo de error de una respuesta JSON ({"error": "..."} o {"error": {"message": "..."}})."""
    if to != "en" or not isinstance(obj, dict) or "error" not in obj:
        return obj
    e = obj["error"]
    if isinstance(e, str):
        return dict(obj, error=tr(e, "en"))
    if isinstance(e, dict) and isinstance(e.get("message"), str):
        return dict(obj, error=dict(e, message=tr(e["message"], "en")))
    return obj
