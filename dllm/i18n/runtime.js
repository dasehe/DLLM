/* SPDX-License-Identifier: AGPL-3.0-or-later · Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
   Traductor de las páginas de DLLM: el texto fuente es español; en inglés (por defecto) se sustituyen los textos del DOM
   con window.__I18N (español → inglés). Elementos con class="notr" (chat, registros de usuario, código) no se tocan. */
(function () {
  var D = window.__I18N || {}, KEYS = null;
  var RE = (window.__I18N_RE || []).map(function (p) { return [new RegExp(p[0]), p[1]]; });
  function getLang() {
    try {
      var q = new URLSearchParams(location.search).get('lang');
      if (q === 'en' || q === 'es') { localStorage.setItem('dllm_lang', q); return q; }
      return localStorage.getItem('dllm_lang') || 'en';
    } catch (e) { return 'en'; }
  }
  var LANG = getLang();
  window.DLLM_LANG = LANG;
  document.documentElement.lang = LANG === 'es' ? 'es-ES' : 'en';
  window.setLang = function (l) { try { localStorage.setItem('dllm_lang', l); } catch (e) {} location.reload(); };
  // texto suelto para el código JS (alert, confirm, mensajes construidos a mano)
  window.L = function (s) { return LANG === 'en' ? String(s).split('\n').map(tr).join('\n') : s; };  // conserva saltos de línea
  function sel() { var s = document.getElementById('langsel'); if (s) s.value = LANG; }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sel); else sel();
  if (LANG !== 'en') return;

  function keys() {  // claves para sustitución parcial (textos con valores dentro): las largas primero
    if (!KEYS) KEYS = Object.keys(D).filter(function (k) { return k.length >= 5 && /\s/.test(k.trim()); })
      .sort(function (a, b) { return b.length - a.length; });
    return KEYS;
  }
  function tr(s) {
    var t = s.trim();
    if (!t) return s;
    if (Object.prototype.hasOwnProperty.call(D, t)) return s.replace(t, D[t]);
    var c = t.replace(/\s+/g, ' ');  // el HTML junta espacios y saltos de línea; el diccionario también
    if (c !== t && Object.prototype.hasOwnProperty.call(D, c)) return s.replace(t, D[c]);
    if (!/[a-záéíóúñ]{2}/i.test(t)) return s;
    var lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0], out = /\n/.test(t) ? c : t, ks = keys();
    for (var i = 0; i < ks.length; i++) {
      if (out.indexOf(ks[i]) >= 0) out = out.split(ks[i]).join(D[ks[i]]);
    }
    for (var r = 0; r < RE.length; r++) out = out.replace(RE[r][0], RE[r][1]);
    return out === (/\n/.test(t) ? c : t) ? s : lead + out + trail;
  }
  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };
  function skip(el) {
    for (; el && el !== document.body; el = el.parentElement) {
      if (SKIP[el.tagName] || (el.classList && el.classList.contains('notr'))) return true;
    }
    return false;
  }
  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
  function walk(root) {
    if (root.nodeType === 3) { if (!skip(root.parentElement)) { var v = tr(root.nodeValue); if (v !== root.nodeValue) root.nodeValue = v; } return; }
    if (root.nodeType !== 1) return;
    if ((root.tagName === 'TEXTAREA' || root.tagName === 'INPUT') && !skip(root.parentElement)) {
      var ph = root.getAttribute('placeholder'), ti = root.getAttribute('title');
      if (ph && tr(ph) !== ph) root.setAttribute('placeholder', tr(ph));
      if (ti && tr(ti) !== ti) root.setAttribute('title', tr(ti));
    }
    if (skip(root)) return;
    var de = root.getAttribute && root.getAttribute('data-en');  // textos ambiguos («Descargar» = descargar/sacar de memoria)
    if (de) { if (root.textContent !== de) root.textContent = de; return; }
    for (var a = 0; a < ATTRS.length; a++) {
      var at = root.getAttribute && root.getAttribute(ATTRS[a]);
      if (at) { var tv = tr(at); if (tv !== at) root.setAttribute(ATTRS[a], tv); }
    }
    if (root.tagName === 'INPUT' && (root.type === 'button' || root.type === 'submit') && root.value) root.value = tr(root.value);
    for (var c = root.firstChild; c; c = c.nextSibling) walk(c);
  }
  var _alert = window.alert, _confirm = window.confirm, _prompt = window.prompt;
  window.alert = function (m) { return _alert.call(window, tr(String(m))); };
  window.confirm = function (m) { return _confirm.call(window, tr(String(m))); };
  window.prompt = function (m, d) { return _prompt.call(window, tr(String(m)), d); };
  function start() {
    document.title = tr(document.title);
    walk(document.body);
    new MutationObserver(function (ms) {
      ms.forEach(function (m) {
        if (m.type === 'characterData') walk(m.target);
        else if (m.type === 'attributes') walk(m.target);
        else m.addedNodes.forEach(walk);
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
