# Development

**English** · [Español](es/DESARROLLO.md)

## Layout

```
dllm/                 Python package (standard library only)
  cli.py              entry point and subcommands
  agent.py            node agent
  orchestrator.py     orchestrator, API, recovery
  scheduler.py        splitting
  gguf.py  hw.py      model metadata, hardware
  auth.py  util.py    HMAC, handshake, tunnel
  config.py           config.json and join code
  installer.py        llama.cpp download, services, diagnostics
  wizard.py           wizard and uninstaller (terminal and web)
  backup.py           full configuration backup/restore
  pairing.py          pairing nodes without a code (DH + verification code)
  presence.py         open tabs and hidden console (`dllm open`, wizard)
  models.py telemetry.py devices.py limits.py   models, monitor, devices, limits
  i18n/               en.json, en.re.json, en.tpl.json, runtime.js: English by default, Spanish (Spain) optional
  ui.html  setup.html  uninstall.html   panel, web wizard and uninstaller
packaging/            launchers and README of the distributable package
scripts/              build_package.py, install.sh / install.ps1, i18n_extract.py, build-llamacpp.sh, systemd example
tests/                unittest + simulated binaries (mockbin/) + fake GGUF generator
docs/                 documentation in English · docs/es/ in Spanish
.github/workflows/    tests.yml (CI, manual only, not used) · release.yml (publishing)
```

## Environment

```bash
git clone https://github.com/dasehe/dllm && cd dllm
python -m dllm --help          # nothing to install
```

To test without real llama.cpp, point `--bin-dir` at `tests/mockbin` (they simulate `rpc-server` and `llama-server`).
With real llama.cpp: `./scripts/build-llamacpp.sh cpu` and `--bin-dir llama.cpp/build/bin`.

Two "computers" on the same machine: use a different `DLLM_HOME` and different ports:
```bash
DLLM_HOME=/tmp/a python -m dllm setup --tui --yes --role both --no-service --bin-dir tests/mockbin
DLLM_HOME=/tmp/b python -m dllm setup --tui --yes --role node --no-service --bin-dir tests/mockbin \
  --join "$(DLLM_HOME=/tmp/a python -m dllm join-code)" --agent-port 8766 --rpc-port 50052
DLLM_HOME=/tmp/a python -m dllm run & DLLM_HOME=/tmp/b python -m dllm run &
```

## Tests

```bash
python -m unittest discover -s tests -v
```

| File | Covers |
|---|---|
| `test_core.py` | GGUF, scheduler, HMAC, handshake, beacons |
| `test_e2e.py` | 3 agents + orchestrator: split, tunnels, API, streaming, authentication, recovery, metrics (POSIX) |
| `test_setup.py` | Package selection, safe extraction, sha256, services, join code, web wizard, full install with the `.pyz` (Linux) |
| `test_i18n.py` | Every visible text has an English translation; terminal and API in English by default |

The table lists the main ones; the rest are in `tests/`. Tests run **locally** before every version (Linux in the
development sessions; Windows and macOS are tested by the owner). Tests check messages in the source language
(`tests/test_00_idioma.py` sets `DLLM_LANG=es`). The `tests` workflow on GitHub is **manual only** and is not used.

Useful variables: `DLLM_HOME` (configuration folder), `DLLM_BIN_DIR`, `DLLM_RELEASE_FILE` (JSON in GitHub releases API
format, to install offline or in tests), `DLLM_LANG` (`en`/`es`).

## Building the package

```bash
python scripts/build_package.py            # → dist/dllm-package-<version>/ and .zip (+ SHA256SUMS)
```
It packs `dllm/` into a `dllm.pyz` (zipapp) and adds the launchers from `packaging/`.

## Releasing a version

1. Update the version in `dllm/__init__.py` **and** `pyproject.toml`.
2. Add the `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md` (it is the release text).
3. Update the affected documentation **in English and Spanish** (`docs/` and `docs/es/`, `README.md` and `README.es.md`).
4. `python -m unittest discover -s tests` green.
5. Commit and push to `main`.
6. On GitHub: **Actions → release → Run workflow** (branch `main`). The workflow takes the version from
   `dllm/__init__.py`, checks that the release does not exist yet, runs the tests, builds the package and creates the
   `vX.Y.Z` tag and the release with the `.zip`, `SHA256SUMS` and the CHANGELOG section as notes.
   Equivalent alternative: `git tag vX.Y.Z && git push origin vX.Y.Z` (the tag must match the version).

**Small versions (since 0.7.0):** every change or small group of changes is published with its own version: a bug fix
or minimal adjustment → patch version (0.7.1, 0.7.2…); new functionality → minor version (0.8.0).

Semantic versioning: incompatible changes in configuration, join code or agent protocol → minor version while on 0.x,
documented in the CHANGELOG with upgrade instructions.

## Languages (English by default, Spanish (Spain) optional)

The pages (`ui.html`, `setup.html`, `uninstall.html`) are written in Spanish and shown **in English by default**: the
server inserts at the `<!--I18N-->` marker the dictionary `dllm/i18n/en.json` (Spanish → English), the expressions of
`dllm/i18n/en.re.json` (texts with values, e.g. "6.0 GiB libres") and `dllm/i18n/runtime.js`, which translates the DOM
and `alert/confirm`. The user picks the language with the header selector (remembered by the browser; `?lang=es` too).

- **Every new visible text needs its entry in `en.json`**: `python scripts/i18n_extract.py --json` lists the missing ones
  and `tests/test_i18n.py` fails if any is left.
- **Sentences with values inside** (f-strings such as `f"nodo {n.name} de nuevo en línea"`, JS templates `${…}`) go
  as whole templates in `dllm/i18n/en.tpl.json` (`"nodo {0} de nuevo en línea": "node {0} back online"`), so they are
  never half translated; the values are translated too when they are known text. `python scripts/i18n_extract.py
  --templates` lists the ones that would be left half in Spanish and `tests/test_i18n.py` fails if any is left.
- Ambiguous texts: `data-en="Unload"` on the element fixes the translation (e.g. "Descargar" = download / unload).
- User content (chat, code): `class="notr"`, not translated.
- Python: messages are written in Spanish and `dllm/i18n` translates them on output (terminal: `install_terminal()` wraps
  stdout/stderr and argparse help; API: `tr_error()` according to `X-DLLM-Lang`/`?lang=`). Language: `--lang`,
  `DLLM_LANG`, `lang` in `config.json`, English by default.
- Documentation: English in `README.md` and `docs/`, Spanish in `README.es.md` and `docs/es/`; both are updated in the
  same commit.

## License in the code

- Every code file starts with:
  ```python
  # SPDX-License-Identifier: AGPL-3.0-or-later
  # Copyright (C) 2026 David Serrano Hernando y colaboradores de DLLM
  ```
  `tests/test_license.py` fails if it is missing.
- The legal notices shown by the interfaces come from `dllm/__init__.py` (`ATTRIBUTION`, `COPYRIGHT`, `SOURCE_URL`,
  `legal_notice()`) and the footer of the pages. Do not remove them: section 7.b of the license requires keeping the
  attribution, and section 13 offering the source code to network users.
- Commits signed with `git commit -s` (DCO). See [CONTRIBUTING.md](../CONTRIBUTING.md).

## Conventions

- No external dependencies in `dllm/` (it must work with a freshly installed Python).
- Messages to the user clear and with the action to take (source in Spanish, translated with `dllm/i18n`).
- Any network input is validated before use; nothing listens outside loopback without authentication.
- Every visible change updates `docs/`, `docs/es/` and `CHANGELOG.md` in the same commit.
