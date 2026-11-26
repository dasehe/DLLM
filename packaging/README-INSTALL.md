# Install DLLM

[Español: LEEME-INSTALAR.md](LEEME-INSTALAR.md)

This package is **the same on every computer**. The wizard asks which role each one will have. The interface is in
English by default; switch to Spanish (Spain) with the selector at the top of the wizard.

## Before you start
- Python 3.9+ (the launchers offer to install it if missing).
- Computers on the same local network, ideally at **1 Gbps or more**.
- Internet during installation (downloads llama.cpp from the official releases). Offline: `--bin-dir FOLDER` with
  llama.cpp already installed (it must include `llama-server` and `ggml-rpc-server`, or `rpc-server` in old builds).
- **Windows 11:** if *Smart App Control* is on, it blocks llama.cpp (its DLLs are not signed). Turn it off in Windows
  Security → App & browser control before installing.

## With a single command (without downloading the package)

- Linux/macOS: `bash <(curl -fsSL https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.sh)`
- Windows (PowerShell): `irm https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.ps1 | iex`

## In 3 steps

| System | How to open the wizard |
|---|---|
| Windows | Double-click **Install-DLLM.bat** |
| macOS | Double-click **Install-DLLM.command** (if it is blocked: right-click → Open) |
| Linux | `./install.sh` (in a terminal without a desktop it opens the text wizard; force with `--tui` or `--gui`) |

1. **On the main computer** → choose *Orchestrator* (or *Both* if you also want it to lend its CPU/GPU) and set the
   **administrator password** (user `admin`; empty = `admin`/`admin`, change it later). At the end it shows a **join
   code** (`DLLM1-…`) and the panel address.
2. **On every computer you want to add** → open the same wizard, choose *Node*, paste the code and choose how much it
   shares: *Automatic* (managed by the orchestrator from its panel) or *Limit from this computer* (e.g. 33 %).
3. Open the orchestrator's panel, check that you see the nodes, add a model in **Add models** (Hugging Face or Ollama),
   pick it and press **Load**.

The join code is a **cluster password**: do not publish it. Nodes only accept connections from the orchestrator.

## Afterwards
```
dllm doctor              # local diagnostics
dllm join-code           # see the join code again (on the orchestrator; also Nodes → 🔑 Join code in the panel)
dllm uninstall [--purge] # uninstall (with --purge also deletes configuration and llama.cpp)
```
(`dllm` = `python ~/.dllm/dllm.pyz`; on Windows `py -3 %USERPROFILE%\.dllm\dllm.pyz`. Spanish output: `dllm --lang es …`.)

## Notes
- **DLLM folder** (default `~/DLLM`): llama.cpp and the models go there. Configuration and secret stay in `~/.dllm`.
- **Firewall:** on Windows the wizard opens only the required ports, limited to your network (asks for administrator
  permission once). On Linux/macOS it shows the exact commands.
- **Node without a join code:** leave it empty in the wizard and claim it from the orchestrator's panel (Nodes →
  Computers waiting to join); confirm the verification code on the node. If it does not appear, give the orchestrator's
  IP in the node's wizard, or use Nodes → Add by IP / Search the network on the orchestrator.
- **Opening the panel:** *DLLM* shortcut (or `Open-DLLM.bat`); the console hides and reappears if you close the browser.
- **Uninstalling:** `Uninstall-DLLM.bat` (Windows), `Uninstall-DLLM.command` (macOS) or `./uninstall.sh` (Linux): a
  separate program from the installer (on an orchestrator it offers to save a backup of the configuration first).
- **Recovering an orchestrator:** install it and in the first step choose *Restore from a backup* (`.dllmbak` file).
- All computers use the **same llama.cpp version**: the orchestrator sets it in the join code.
- Linux with an NVIDIA GPU: the official CUDA package is used (driver with CUDA ≥ 12.4); if not possible, the wizard
  tries Vulkan and then CPU. For your own build: `./build-llamacpp.sh cuda` and `--bin-dir`.
- Traffic between nodes is **authenticated but not encrypted**: use it on a trusted network or a VPN.

## More information
Full documentation (usage, API, security, troubleshooting): https://github.com/dasehe/dllm/tree/main/docs

## License
DLLM — original idea and initial development by David Serrano Hernando. Free software under GNU AGPL-3.0-or-later (see
`LICENSE` and `NOTICE`).
