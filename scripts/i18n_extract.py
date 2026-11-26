#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
"""Extrae los textos visibles (en español, idioma fuente) de las páginas HTML para el diccionario dllm/i18n/en.json.
   python scripts/i18n_extract.py            → lista los textos sin traducir
   python scripts/i18n_extract.py --json     → JSON {texto: ""} con los que faltan (para añadirlos al diccionario)
Las páginas siguen escritas en español; el panel se muestra en inglés por defecto traduciendo con el diccionario."""
import html as htmllib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["dllm/ui.html", "dllm/setup.html", "dllm/uninstall.html"]
DICT = ROOT / "dllm" / "i18n" / "en.json"

WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ¿¡]")
CODE = re.compile(r"^[\w.#\-\[\]=:>~*+]*[.#\[\]=:>~*+][\w.#\-\[\]=:>~*+]*$|^/|^https?:|\(\)|=>|^[a-z]+[A-Z]\w*$|\.(png|js|json|html|gguf)$|^--?\w")
SKIP_NOTR = re.compile(r"<!--notr-->.*?<!--/notr-->", re.S)


def js_literals(js):
    """Tokenizador mínimo de JS: devuelve los trozos estáticos de cada literal ('', "", `` con ${} anidados)."""
    out, i, n = [], 0, len(js)

    def template(i):  # i apunta tras la comilla invertida; devuelve (trozos, fin)
        pieces, buf = [], ""
        while i < n:
            c = js[i]
            if c == "\\":
                buf += js[i:i + 2]
                i += 2
            elif c == "`":
                pieces.append(buf)
                return pieces, i + 1
            elif js.startswith("${", i):
                pieces.append(buf)
                buf = ""
                i = code(i + 2, "}")
            else:
                buf += c
                i += 1
        pieces.append(buf)
        return pieces, i

    def code(i, end):  # recorre código hasta la llave de cierre, recogiendo literales anidados
        depth = 0
        while i < n:
            c = js[i]
            if c in "'\"":
                j = i + 1
                while j < n and js[j] != c and js[j] != "\n":
                    j += 2 if js[j] == "\\" else 1
                out.append(("q", js[i + 1:j]))
                i = j + 1
            elif c == "`":
                ps, i = template(i + 1)
                out.append(("t", ps))
            elif c == "{":
                depth += 1
                i += 1
            elif c == "}":
                if depth == 0 and end == "}":
                    return i + 1
                depth -= 1
                i += 1
            elif c == "/" and i + 1 < n and js[i + 1] == "/":
                i = js.find("\n", i)
                i = n if i < 0 else i
            else:
                i += 1
        return i

    code(0, None)
    res = []
    for kind, v in out:
        for piece in ([v] if kind == "q" else v):
            res.append(piece)
    return res


def _visible(piece):
    """Texto visible de un trozo con HTML (puede empezar o acabar a mitad de una etiqueta)."""
    attrs = re.findall(r'\b(?:title|placeholder|aria-label|alt)="([^"$]+)"', piece)
    lt, gt = piece.find("<"), piece.find(">")
    if gt >= 0 and (lt < 0 or gt < lt) and re.search(r'["=]', piece[:gt]):
        piece = piece[gt + 1:]  # empezaba dentro de una etiqueta
    elif piece.startswith(">"):
        piece = piece[1:]
    k = piece.rfind("<")
    if k >= 0 and ">" not in piece[k:]:
        piece = piece[:k]       # acaba dentro de una etiqueta
    return re.split(r"<[^>]*>", piece) + attrs


def _keep(s, html=False):
    s = s.strip()
    if re.fullmatch(r"[A-ZÁÉÍÓÚÑ¿¡][\wáéíóúñ ]*:", s):
        return s  # «Instalado:», «Base URL:»
    if len(s) < 2 or not WORD.search(s) or CODE.search(s):
        return None
    if not html and re.fullmatch(r"[\w\-]+", s) and not re.search(r"[A-ZÁÉÍÓÚÑáéíóúñ]", s):
        return None  # identificadores, clases CSS, ids
    if re.search(r'="|=\'|\\\\|^#|^"|^\'|onclick|\bstyle=|stroke-|api_key=|-H "', s):
        return None  # restos de atributos o de código dentro de literales
    if re.search(r"[{}]|;\s*\w+\s*[=(.]|\bfunction\s*\w*\s*\(|\bconst \w+\s*=|\breturn\b[^.]*;|\$\(|getElementById|\.classList", s):
        return None
    if re.fullmatch(r"[\d\s.,:%/·×→←+\-()]*(GiB|MiB|MB|KB|GB|ms|s|tok/s|Mbps|Gbps|W|°C|%)?", s):
        return None
    return s


def extract(text):
    text = SKIP_NOTR.sub("", text)
    a, b = text.find("<script>"), text.rfind("</script>")
    html, js = (text[:a] + text[b:], text[a:b]) if a >= 0 else (text, "")
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    found = set()
    for m in re.findall(r">([^<>]+)<", re.sub(r"<style>.*?</style>", "", html, flags=re.S)):
        k = _keep(htmllib.unescape(" ".join(m.split())), html=True)  # el navegador junta los espacios y saltos de línea
        if k:
            found.add(k)
    for m in re.findall(r'\b(?:title|placeholder|aria-label|alt|data-tip)="([^"]+)"', text):
        if "${" not in m and _keep(htmllib.unescape(m)):
            found.add(_keep(htmllib.unescape(m)))
    for lit in js_literals(js):
        if re.search(r"^\s*[.#]?[\w-]+\s*\{|\{\s*[\w-]+\s*:", lit):
            continue  # CSS
        for i, p in enumerate(_visible(lit)):
            for line in p.replace("\\n", "\n").split("\n"):
                k = _keep(line)
                if k and (" " in k or re.search(r"[áéíóúñ¿¡]|^[A-ZÁÉÍÓÚ¿¡][a-záéíóúñ]+", k)):
                    found.add(k)
    return found


PY_SP = re.compile(r"[áéíóúñ¿¡]|\b(el|la|los|las|de|del|no|un|una|con|sin|para|por|que|se|nodo|nodos|modelo|modelos|"
                   r"equipo|carga|falta|puerto|hay|está|usa|ya|sobre|desde|hasta|cada|este|esta|todo|solo|inferencia|"
                   r"distribuida|nuevo|nueva|otro|otra|cuando|como|pero|puede|son|es)\b", re.I)
PY_SKIP = {"dllm/legal_notice.py"}  # el aviso legal va en los dos idiomas tal cual


def extract_py(text):
    """Mensajes visibles del código Python (errores, eventos, terminal): literales y trozos de f-strings, sin docstrings."""
    import ast
    tree = ast.parse(text)
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            docs.add(id(n.body[0].value))
    keys = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):  # las claves de diccionario son datos, no texto
            keys |= {id(k) for k in n.keys if k is not None}
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and id(n) not in keys:
            for line in n.value.split("\n"):
                s = line.strip()
                if len(s) >= 3 and " " in s and PY_SP.search(s) and not re.fullmatch(r"[\w./\-]+", s) \
                        and not re.search(r"^[-\w]+=|%\(|\{\w*\}|^<|^\w+\(|\\[sdw]|\$\w+=|^Copyright \(C\)", s):
                    found.add(s)
    return found


def all_strings():
    out = set()
    for p in PAGES:
        out |= extract((ROOT / p).read_text(encoding="utf-8"))
    for f in sorted((ROOT / "dllm").glob("*.py")):
        if str(f.relative_to(ROOT)).replace("\\", "/") not in PY_SKIP:
            out |= extract_py(f.read_text(encoding="utf-8"))
    return out


def missing():
    d = json.loads(DICT.read_text(encoding="utf-8")) if DICT.exists() else {}
    return sorted(s for s in all_strings() if s not in d)


if __name__ == "__main__":
    m = missing()
    if "--json" in sys.argv:
        print(json.dumps({k: "" for k in m}, ensure_ascii=False, indent=1))
    else:
        print("\n".join(m))
        print(f"\n{len(m)} sin traducir", file=sys.stderr)
