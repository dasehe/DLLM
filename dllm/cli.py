# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, auth, config, hw, legal_notice


def _sigterm(*_):
    raise KeyboardInterrupt


# ---------------------------------------------------------------- cliente API
def _api(args, method, path, body=None):
    key = args.api_key or os.environ.get("DLLM_API_KEY")
    if not key:
        cfg = config.load()
        key = ((cfg or {}).get("orchestrator") or {}).get("api_key")
    req = urllib.request.Request(args.url.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"error {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"no se pudo contactar con {args.url}: {e.reason}")


def gib(x):
    return f"{(x or 0) / hw.GiB:.1f} GiB"


def print_plan(p):
    print(f"modelo {p['model']} · ctx {p['ctx']} · necesita {gib(p['need_bytes'])} "
          f"(KV {gib(p['kv_bytes'])})")
    if not p["ok"]:
        print("  ✗", p["reason"])
    for a in p["assignments"]:
        print(f"  → {a['name']:<18} {gib(a['bytes']):>9}  {a['share'] * 100:5.1f}%  "
              f"{a['layers']} capas  [{a['backend']}, ~{a['bw_gbps']} GB/s, {a['mbps']} Mbps, {a['rtt_ms']} ms]")
    for e in p["excluded"]:
        print(f"  ✗ {e['node']}: {e['reason']}")
    if p["est_tok_s"]:
        print(f"  ≈ {p['est_tok_s']} tok/s (estimación gruesa)")
    for w in p["warnings"]:
        print("  !", w)


# ---------------------------------------------------------------- comandos
def cmd_init(a):
    s = auth.load_secret()
    if s is None or a.force:
        s = auth.new_secret()
        f = auth.save_secret(s)
        print(f"Secreto del clúster creado en {f}")
    else:
        print("Ya existía un secreto del clúster (usa --force para regenerarlo)")
    print(f"\nEn cada nodo:   dllm agent --secret {s}")
    print("En este equipo: dllm up --models-dir ~/models")
    print("\n(Más fácil: `dllm setup` hace todo esto con un asistente.)")


def cmd_status(a):
    st = _api(a, "GET", "/api/state")
    print(f"estado: {st['state']}" + (f" · modelo: {st['model']}" if st["model"] else "")
          + (f" · error: {st['error']}" if st["error"] else "") + f" · recuperaciones: {st['recoveries']}")
    for n in st["nodes"]:
        net = n["net"] or {}
        print(f"  {'●' if n['up'] else '○'} {n['name']:<18} {n['host']:<15} {n['backend'] or '-':<4} "
              f"{gib(n['cap_bytes']):>9}  ~{n['bw_gbps']} GB/s  {net.get('mbps', '?')} Mbps"
              f"{'  [en uso]' if n['used'] else ''}")
    if st["plan"]:
        print_plan(st["plan"])


def cmd_plan(a):
    print_plan(_api(a, "POST", "/api/plan", {"model": a.model, "ctx": a.ctx}))


def cmd_load(a):
    body = {"model": a.model, "ctx": a.ctx}
    if a.slot:
        body["slot"] = int(a.slot) if a.slot.isdigit() else a.slot
    if a.on_demand:
        body["pinned"] = False
    _api(a, "POST", "/api/load", body)
    print("carga iniciada; sigue el progreso con `dllm status` o en la UI")


def cmd_unload(a):
    _api(a, "POST", "/api/unload")
    print("modelo descargado")


def _agent(a):
    from . import agent
    agent.run(a)


def _up(a):
    from . import orchestrator
    a.llama_bin = a.llama_bin or hw.find_bin("llama-server", a.bin_dir)
    orchestrator.run(a)


def cmd_run(a):
    """Arranca lo que diga config.json (lo usan los servicios de arranque automático)."""
    cfg = config.load()
    if not cfg or not (cfg.get("secret") or cfg.get("role") == "node"):  # un nodo sin secreto espera emparejarse
        sys.exit("no hay configuración: ejecuta `dllm setup`")
    parser = build_parser()
    role = cfg["role"]
    if role in ("node", "both"):
        ns = parser.parse_args(config.agent_argv(cfg))
        if role == "both":
            threading.Thread(target=_agent, args=(ns,), daemon=True).start()
        else:
            return _agent(ns)
    ns = parser.parse_args(config.up_argv(cfg))
    _up(ns)


def _local_json(port, path, timeout=3):
    import http.client
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        c.close()
        return json.loads(data) if r.status == 200 else None
    except (OSError, ValueError):
        return None


def cmd_open(a):
    """Abre el panel: arranca DLLM si está parado, abre el navegador y (con --hide-console) esconde la consola, que
    vuelve a aparecer si se cierra el navegador con DLLM en marcha, para reabrirlo o pararlo."""
    import webbrowser
    from . import installer, presence
    cfg = config.load()
    if not cfg:
        sys.exit("DLLM no está instalado en este equipo: ejecuta el asistente (Install-DLLM)")
    if cfg["role"] == "node":
        if not cfg.get("secret"):  # aún sin clúster: confirmar aquí el emparejamiento
            from . import wizard
            if _local_pairing_up(cfg) is None:
                installer.start_service()
                time.sleep(3)
            sys.exit(wizard.pair_console(cfg["agent"]["port"]))
        print("Este equipo es un nodo: no tiene panel. Su estado se ve en el panel del orquestador.")
        return
    port = cfg["orchestrator"]["port"]
    child = None
    if _local_json(port, "/healthz") is None:
        print("Arrancando DLLM…", flush=True)
        ok, _ = installer.start_service()
        if not ok:  # sin servicio instalado: se arranca aquí mismo, en segundo plano
            import subprocess
            cmd, env = installer.entry_cmd(installer.install_program() or None)
            child = subprocess.Popen(cmd, env=dict(os.environ, **env), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        for _ in range(60):
            if _local_json(port, "/healthz") is not None:
                break
            time.sleep(0.5)
        else:
            sys.exit("DLLM no ha arrancado: prueba `dllm doctor`")
    url = f"http://127.0.0.1:{port}/"
    open_ = lambda: webbrowser.open(url)
    open_()
    print(f"Panel de DLLM: {url}", flush=True)
    if a.no_watch:
        return
    if a.hide_console:
        presence.hide_console()

    def stop():
        if child:
            child.terminate()
        else:
            installer.stop_service()
        print("DLLM parado. Para volver a usarlo, abre el acceso directo DLLM.")

    res = presence.watch(lambda: _local_json(port, "/api/ui/clients"), open_, "el panel", stop)
    if res == "terminado":
        presence.show_console()
        print("DLLM se ha detenido.")
        time.sleep(3)


def _local_pairing_up(cfg):
    from . import wizard
    try:
        return wizard.local_pairing(cfg["agent"]["port"])
    except RuntimeError:
        return {}


def cmd_pair(a):
    """En un nodo instalado sin código: confirma la solicitud del orquestador que lo reclama."""
    from . import wizard
    cfg = config.load()
    if not cfg or cfg["role"] != "node":
        sys.exit("este equipo no es un nodo de DLLM")
    if cfg.get("secret"):
        print("Este nodo ya pertenece a un clúster. Para cambiarlo, reinstálalo con el asistente.")
        return
    sys.exit(wizard.pair_console(cfg["agent"]["port"]))


def cmd_setup(a):
    from . import wizard
    sys.exit(wizard.main(a))


def cmd_join_code(a):
    from . import wizard
    cfg = config.load()
    if not cfg or cfg["role"] == "node":
        sys.exit("este equipo no es un orquestador: ejecuta el asistente y elige Orquestador")
    print(wizard.Setup.join_code(cfg, a.advertise_host or cfg.get("advertise_host")))


def _passphrase(a, confirm=False):
    import getpass
    if a.passphrase_file:
        return Path(a.passphrase_file).read_text(encoding="utf-8").strip()
    if os.environ.get("DLLM_BACKUP_PASSPHRASE"):
        return os.environ["DLLM_BACKUP_PASSPHRASE"]
    p = getpass.getpass("Contraseña de la copia: ")
    if confirm and p != getpass.getpass("Repite la contraseña: "):
        sys.exit("las contraseñas no coinciden")
    return p


def cmd_backup(a):
    """Copia de seguridad / restauración de toda la configuración, sin necesidad de que el orquestador esté en marcha."""
    from . import backup, models as models_mod
    home = config.home()
    if a.action == "export":
        cfg = config.load() or {}
        inv = []
        if cfg.get("models_dir") and Path(cfg["models_dir"]).is_dir():
            for m in models_mod.Manager(cfg["models_dir"]).list():
                parts = m["name"].split("/")
                inv.append({"name": m["name"], "size": m.get("size"), "source": m.get("source"), "quant": m.get("quant"),
                            "hf": {"repo": parts[1].replace("__", "/"), "file": parts[-1]}
                            if parts[0] == "hf" and len(parts) >= 3 else None})
        pw = None if a.plain else _passphrase(a, confirm=True)
        try:
            blob = backup.build(home, inv, __version__, socket.gethostname(), a.logs, pw)
        except backup.BackupError as e:
            sys.exit(str(e))
        out = Path(a.file or backup.filename(socket.gethostname()))
        out.write_bytes(blob)
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print(f"Copia creada: {out} ({len(blob) // 1024} KiB){' · SIN CIFRAR: contiene el secreto del clúster' if a.plain else ' · cifrada'}")
        return
    try:
        blob = Path(a.file).read_bytes()
        man, files = backup.open_backup(blob, _passphrase(a) if backup.is_encrypted(blob) else None)
    except (OSError, backup.BackupError) as e:
        sys.exit(f"no se pudo abrir la copia: {e}")
    s = backup.summary(man, files, home)
    print(f"Copia de DLLM {s['version']} del {time.strftime('%d/%m/%Y %H:%M', time.localtime(s['created'] or 0))} · "
          f"equipo {s['host']} · rol {s['role']}\n  ajustes de {s['model_settings']} modelo(s), {s['node_settings']} nodo(s), "
          f"{s['nodes_added']} nodo(s) añadidos por IP · token de Hugging Face: {'sí' if s['hf_token'] else 'no'} · "
          f"{len(s['models'])} modelo(s) en el inventario")
    if s["secret_changes"]:
        print("  ATENCIÓN: el secreto del clúster es distinto del de este equipo; los nodos actuales dejarán de conectar "
              "hasta que se reinstalen con el de la copia.")
    if a.action == "inspect":
        return
    if not a.yes and input("¿Restaurar y sustituir la configuración actual? [s/N] ").strip().lower() not in ("s", "si", "sí", "y"):
        return
    actions, safety = backup.restore(files, home)
    print("Restaurado: " + "; ".join(actions))
    if safety:
        print(f"La configuración anterior se guardó en {safety}")
    print("Reinicia DLLM para aplicar el secreto y la configuración.")


def cmd_doctor(a):
    from . import installer
    bad = 0
    for c in installer.doctor(config.load()):
        print(f"  {'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}")
        bad += not c["ok"]
    sys.exit(1 if bad else 0)


def cmd_uninstall(a):
    from . import installer, wizard
    if a.gui and wizard.has_display():
        return wizard.main_uninstall(a)
    if a.gui:
        a.interactive = True  # sin escritorio: preguntas en la terminal
    if a.interactive:
        cfg = config.load()
        if not cfg:
            print("DLLM no está instalado en este equipo (no hay configuración).")
            return
        print(f"DLLM instalado: rol {cfg['role']} · {cfg.get('install_dir') or config.home()}")

        def yn(q, d):
            s = input(f"{q} [{'S/n' if d else 's/N'}] ").strip().lower()
            return d if not s else s in ("s", "si", "sí", "y", "yes")
        if not yn("¿Desinstalar DLLM de este equipo?", False):
            return
        a.purge = yn("¿Borrar también llama.cpp y la configuración?", True)
        a.models = a.purge and cfg["role"] != "node" and yn("¿Borrar también los modelos descargados?", False)
    for line in installer.uninstall_all(purge=a.purge, models=a.models):
        print("·", line)


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(prog="dllm", description="Distributed LLM: inferencia distribuida sobre llama.cpp RPC",
                                epilog=legal_notice(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"dllm {__version__}\n{legal_notice()}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="asistente de instalación (navegador o terminal)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--gui", action="store_true", help="asistente en el navegador (por defecto si hay escritorio)")
    g.add_argument("--tui", action="store_true", help="asistente en esta terminal")
    s.add_argument("--no-browser", action="store_true", help="no abrir el navegador, solo mostrar la URL")
    s.add_argument("--hide-console", action="store_true",
                   help="Windows: esconde la consola mientras el asistente está abierto en el navegador")
    s.add_argument("--port", type=int, default=0, help="puerto del asistente web (0 = aleatorio)")
    s.add_argument("--role", choices=["orchestrator", "node", "both"])
    s.add_argument("--join", help="código de unión del orquestador (rol nodo)")
    s.add_argument("--install-dir", help="carpeta de DLLM: llama.cpp y modelos (por defecto ~/DLLM)")
    s.add_argument("--models-dir", help="carpeta de modelos (por defecto <carpeta de DLLM>/models)")
    s.add_argument("--backend", choices=["auto", "cpu", "vulkan", "cuda", "rocm", "sycl", "metal"])
    s.add_argument("--no-firewall", action="store_true", help="no abrir el cortafuegos de Windows automáticamente")
    s.add_argument("--bin-dir", help="usar llama.cpp ya instalado en esta carpeta")
    s.add_argument("--name")
    s.add_argument("--expose", action="store_true", help="orquestador accesible desde la red (crea API key)")
    s.add_argument("--orch-ip", help="(nodo sin código) IP[:puerto de descubrimiento] del orquestador ya activo; "
                                     "el nodo se anuncia a esa IP en vez de por broadcast")
    s.add_argument("--advertise-host", help="IP del orquestador que irá en el código de unión (p. ej. la de WireGuard)")
    s.add_argument("--no-service", action="store_true", help="no instalar el arranque automático")
    s.add_argument("--agent-port", type=int, help="puerto HTTP del agente (def. 8765)")
    s.add_argument("--rpc-port", type=int, help="puerto del proxy RPC (def. 50051)")
    s.add_argument("--api-port", type=int, help="puerto de la API/panel del orquestador (def. 8080)")
    s.add_argument("--beacon-port", type=int, help="puerto UDP de descubrimiento (def. 50999)")
    s.add_argument("--mem-gb", type=float, help="memoria a ofrecer en GiB (def. recomendado)")
    s.add_argument("--share", type=float, help="porcentaje del equipo a compartir, 1-100 (def. recomendado)")
    s.add_argument("--threads", type=int, help="hilos de CPU a compartir (def. recomendado)")
    s.add_argument("--limits-mode", choices=["auto", "manual"],
                   help="auto: lo gestiona el orquestador (def.); manual: fijado en este nodo. "
                        "Si das --share/--mem-gb/--threads sin este flag, se usa manual")
    s.add_argument("--bw-gbps", type=float, help="ancho de banda de memoria a declarar (def. estimar)")
    s.add_argument("--yes", action="store_true", help="sin preguntas: acepta los valores por defecto")
    s.add_argument("--dry-run", action="store_true", help="simula: no descarga ni escribe nada")
    s.set_defaults(fn=cmd_setup)

    sub.add_parser("run", help="arranca según config.json (rol del equipo)").set_defaults(fn=cmd_run)
    s = sub.add_parser("join-code", help="muestra el código de unión para nodos")
    s.add_argument("--advertise-host", help="IP a incluir en el código (por defecto la de la LAN)")
    s.set_defaults(fn=cmd_join_code)
    sub.add_parser("doctor", help="diagnóstico local").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("backup", help="copia de seguridad y restauración de toda la configuración")
    s.add_argument("action", choices=["export", "import", "inspect"])
    s.add_argument("file", nargs="?", help="export: fichero de salida (def. dllm-<equipo>-<fecha>.dllmbak); "
                                           "import/inspect: fichero de la copia")
    s.add_argument("--passphrase-file", help="fichero con la contraseña (si no, se pide; o DLLM_BACKUP_PASSPHRASE)")
    s.add_argument("--plain", action="store_true", help="export: SIN cifrar (contiene el secreto del clúster)")
    s.add_argument("--logs", action="store_true", help="export: incluye también los registros (events.jsonl)")
    s.add_argument("--yes", action="store_true", help="import: no preguntar")
    s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("pair", help="nodo sin código de unión: confirmar el orquestador que lo reclama")
    s.set_defaults(fn=cmd_pair)

    s = sub.add_parser("open", help="abre el panel (arranca DLLM si está parado)")
    s.add_argument("--hide-console", action="store_true",
                   help="Windows: esconde esta consola; reaparece si cierras el navegador con DLLM en marcha")
    s.add_argument("--no-watch", action="store_true", help="solo abrir el navegador y salir")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("uninstall", help="desinstala DLLM de este equipo (agente y/o orquestador)")
    s.add_argument("--purge", action="store_true", help="borra también configuración y llama.cpp descargado")
    s.add_argument("--models", action="store_true",
                   help="con --purge: borra también la carpeta de modelos si la creó DLLM")
    s.add_argument("--interactive", action="store_true", help="pregunta qué borrar en esta terminal")
    s.add_argument("--gui", action="store_true", help="desinstalador en el navegador (lo usan Uninstall-DLLM y uninstall.sh)")
    s.add_argument("--no-browser", action="store_true", help="con --gui: no abrir el navegador, solo mostrar la URL")
    s.add_argument("--hide-console", action="store_true", help="con --gui (Windows): esconde la consola")
    s.add_argument("--port", type=int, default=0, help="con --gui: puerto (0 = aleatorio)")
    s.add_argument("--dry-run", action="store_true", help="con --gui: simula sin borrar nada")
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("init", help="crea el secreto del clúster (modo manual)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("agent", help="arranca el agente de nodo")
    s.add_argument("--secret", default=auth.load_secret())
    s.add_argument("--name")
    s.add_argument("--port", type=int, default=8765, help="API HTTP del agente")
    s.add_argument("--rpc-port", type=int, default=50051, help="proxy RPC autenticado")
    s.add_argument("--bin-dir", help="carpeta con rpc-server")
    s.add_argument("--rpc-bin")
    s.add_argument("--backend", choices=["auto", "cpu", "gpu"], default="auto")
    s.add_argument("--gpu-index", type=int, default=0, help=argparse.SUPPRESS)  # obsoleto: usa --device
    s.add_argument("--device", help="dispositivo a prestar (p. ej. CUDA0, Vulkan1 o parte de su nombre); "
                                    "por defecto el acelerador con más memoria")
    s.add_argument("--llama-backend", help="paquete de llama.cpp a usar al actualizar (cpu, cuda, vulkan, rocm, metal); "
                                           "por defecto el que se instaló")
    s.add_argument("--install-dir", help="carpeta de DLLM (llama.cpp se actualiza en <carpeta>/llama.cpp)")
    s.add_argument("--no-auto-update", action="store_true",
                   help="no permitir que el orquestador actualice llama.cpp en este nodo")
    s.add_argument("--limits-mode", choices=["auto", "manual"], default="auto",
                   help="auto: el orquestador puede ajustar lo que aporta este nodo; manual: solo aquí")
    s.add_argument("--share", type=float, help="porcentaje del equipo a compartir (RAM/VRAM e hilos), 1-100")
    s.add_argument("--mem-gb", type=float, help="memoria a ofrecer en GiB (tiene prioridad sobre --share)")
    s.add_argument("--reserve-gb", type=float, default=1.0, help="memoria a dejar libre para el SO")
    s.add_argument("--config-file", help=argparse.SUPPRESS)  # lo pasa `dllm run` para guardar cambios
    s.add_argument("--bw-gbps", type=float, help="fuerza el ancho de banda de memoria estimado")
    s.add_argument("--threads", type=int, help="hilos de CPU para rpc-server (tiene prioridad sobre --share)")
    s.add_argument("--cache", action="store_true", help="rpc-server -c: caché local de tensores")
    s.add_argument("--rpc-arg", action="append", help="argumento extra para rpc-server (repetible)")
    s.add_argument("--allow-ip", action="append", help="solo aceptar conexiones de esta IP (repetible)")
    s.add_argument("--announce-to", action="append", help="HOST[:PUERTO] del orquestador (si no, broadcast)")
    s.add_argument("--beacon-port", type=int, default=50999)
    s.add_argument("--beacon-interval", type=float, default=3.0)
    s.add_argument("--pairing", action="store_true",
                   help="sin secreto: anunciarse para que un orquestador lo reclame (confirmar con `dllm pair`)")
    s.add_argument("--no-restrict-pair", dest="restrict_pair", action="store_false",
                   help="tras emparejarse, aceptar conexiones de cualquier IP (por defecto solo del orquestador)")
    s.set_defaults(fn=_agent)

    s = sub.add_parser("up", help="arranca el orquestador (API + UI)")
    s.add_argument("--secret", default=auth.load_secret())
    s.add_argument("--models-dir", default=os.path.expanduser("~/models"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--api-key", help="obligatoria si --host no es loopback; 'auto' la genera")
    s.add_argument("--advertise-host", help="IP de este equipo para los nodos (p. ej. la de WireGuard)")
    s.add_argument("--node", action="append", help="HOST[:PUERTO] de un agente (repetible)")
    s.add_argument("--bin-dir", help="carpeta con llama-server")
    s.add_argument("--llama-bin")
    s.add_argument("--llama-arg", action="append", help="argumento extra para llama-server (repetible)")
    s.add_argument("--no-direct-local", action="store_true",
                   help="no ejecutar sin RPC los modelos que caben en la GPU local (por defecto sí)")
    s.add_argument("--min-mbps", type=float, default=800.0, help="red mínima orquestador↔nodo (def. 800)")
    s.add_argument("--on-demand", action="store_true",
                   help="cargar bajo demanda el modelo pedido en «model» si está en la carpeta de modelos "
                        "(expulsa por LRU los modelos no fijados si falta memoria)")
    s.add_argument("--allow-ip", action="append", default=[],
                   help="solo aceptar peticiones de esta IP además de loopback (repetible)")
    s.add_argument("--vram-safety-mb", type=int, default=256,
                   help="margen de VRAM por nodo cuando ya hay otro modelo en él (def. 256)")
    s.add_argument("--ram-safety-mb", type=int, default=1024,
                   help="margen de RAM que se deja libre al SO en cada equipo (def. 1024)")
    s.add_argument("--queue-timeout", type=float, default=30.0,
                   help="s que espera una petición que necesita cargar un modelo si la memoria está ocupada (def. 30)")
    s.add_argument("--beacon-port", type=int, default=50999)
    s.add_argument("--node-timeout", type=float, default=10.0)
    s.add_argument("--probe-interval", type=float, default=3.0)
    s.add_argument("--load-timeout", type=float, default=1800.0)
    s.add_argument("--no-auto-sync", action="store_true",
                   help="no pedir a los nodos que instalen la misma versión de llama.cpp que el orquestador")
    s.set_defaults(fn=_up)

    for name, fn, help_ in (("status", cmd_status, "estado y nodos"), ("plan", cmd_plan, "simula el reparto"),
                            ("load", cmd_load, "carga un modelo"), ("unload", cmd_unload, "descarga el modelo")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--url", default="http://127.0.0.1:8080")
        s.add_argument("--api-key")
        if name in ("plan", "load"):
            s.add_argument("model", help="nombre del .gguf relativo a --models-dir")
            s.add_argument("--ctx", type=int, default=4096)
        if name == "load":
            s.add_argument("--slot", help="número de slot o «new» para cargarlo junto a los que ya hay")
            s.add_argument("--on-demand", action="store_true", help="no fijar el modelo: podrá expulsarse por LRU")
        s.set_defaults(fn=fn)
    return p


def main():
    from . import i18n
    if "--lang" in sys.argv[1:-1]:  # opción global: dllm --lang es status
        i = sys.argv.index("--lang")
        os.environ["DLLM_LANG"] = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    i18n.install_terminal()
    a = build_parser().parse_args()
    hw.quiet_windows_errors()
    hw.hide_child_consoles()
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        pass
