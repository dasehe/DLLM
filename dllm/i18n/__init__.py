# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Idiomas de las interfaces. Las páginas se escriben en español (idioma fuente) y se muestran en inglés por defecto:
el navegador traduce el DOM con el diccionario `en.json` (español → inglés); el usuario puede elegir Español (España).
Cada texto visible nuevo debe tener su entrada en `en.json` (lo comprueba tests/test_i18n.py; `scripts/i18n_extract.py`
lista los que faltan)."""
import json
import pkgutil

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
    return html.replace("<!--I18N-->", f"<script>window.__I18N={data};window.__I18N_RE={pats};</script>"
                                       f"<script>{js}</script>", 1)


def patterns(lang="en"):
    """Expresiones regulares para textos con valores dentro (p. ej. «de 6.0 GiB»): [[patrón, sustitución], ...]."""
    raw = pkgutil.get_data("dllm", f"i18n/{lang}.re.json")
    return json.loads(raw.decode("utf-8")) if raw else []
