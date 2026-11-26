# Installation

**English** · [Español](es/INSTALACION.md)

The **same package** works for every computer; the wizard asks which role each one will have.

| Role | What it does | How many |
|---|---|---|
| **Orchestrator** | Stores the models, splits the load, serves the panel and the API. Does not contribute compute. | One per cluster |
| **Node** | Lends its CPU/GPU and memory to the orchestrator. | As many as you want |
| **Both** | Orchestrator that also contributes its own CPU/GPU as one more node. | One per cluster |

## 1. Requirements

| | Minimum | Recommended |
|---|---|---|
| System | Windows 10/11, macOS 12+, Linux (Ubuntu 22.04+, Debian 12+, Fedora…) | — |
| Python | 3.9 | 3.11+ |
| Network | 1 Gbps between orchestrator and nodes (cable) | 2.5/10 Gbps, same subnet |
| Memory | Sum of the nodes' free RAM/VRAM ≥ model size + ~10 % | 2 GiB margin per node |

The scheduler excludes nodes measured below **800 Mbps** (Wi-Fi usually falls out). It can be changed with
`--min-mbps`, at the cost of much more slowness. For testing, in **Nodes** you can allow a specific slow node (*Use
anyway*, asks for confirmation) without touching the minimum for the rest.

## 2. Get the package

### Install with a single command

**Linux and macOS** (terminal):
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.sh)
```
**Windows** (PowerShell, no need to run it as administrator):
```powershell
irm https://raw.githubusercontent.com/dasehe/DLLM/main/scripts/install.ps1 | iex
```

Both install Python 3 if missing (the distribution's package manager / `brew` / `winget`), download the `main` branch as
an archive (no git needed) into `~/dllm-src` (`%USERPROFILE%\dllm-src` on Windows), build the package and open the
wizard. On Linux over SSH the text wizard is used (`--tui`). Variables: `DLLM_SRC` (folder), `DLLM_REF` (branch or tag,
e.g. `v0.8.0`), `DLLM_LANG` (`en`/`es`), `GH_TOKEN` (only if the repository were private; sent in a header and never
stored). To update, run the same command again. `scripts/instalar-ubuntu.sh` still works and redirects to `install.sh`.

> Review the script before running it if you prefer: `curl -fsSL …/install.sh | less` or `irm …/install.ps1`.

### Package by hand

Build `dllm-package-<version>.zip` with `python scripts/build_package.py` (or download it from
[Releases](https://github.com/dasehe/dllm/releases) when one is published), check it against `SHA256SUMS` if you want
and unzip it. Contents:

```
dllm.pyz               the whole program (a single file)
Install-DLLM.bat       wizard for Windows (double-click)
Install-DLLM.command   wizard for macOS (double-click)
install.sh             wizard for Linux (and macOS from a terminal)
Open-DLLM.bat          open the panel on Windows (same as the DLLM shortcut)
Uninstall-DLLM.bat     uninstaller for Windows (double-click)
Uninstall-DLLM.command uninstaller for macOS (double-click)
uninstall.sh           uninstaller for Linux (and macOS from a terminal)
build-llamacpp.sh      build llama.cpp from source (CUDA on Linux, etc.)
README-INSTALL.md      summary of these instructions (LEEME-INSTALAR.md in Spanish)
SHA256SUMS             checksums
```

> How to build the package: see [DEVELOPMENT](DEVELOPMENT.md#building-the-package).

From the repository (Ubuntu / Debian):

```bash
sudo apt update && sudo apt install -y git python3
git clone https://github.com/dasehe/dllm.git && cd dllm
python3 scripts/build_package.py --out dist      # builds dist/dllm-package-<version>/ and the .zip
cd dist/dllm-package-*/
./install.sh                                     # wizard (add --tui over SSH)
python3 ~/.dllm/dllm.pyz doctor                  # diagnostics after installing
```

On the orchestrator choose *Orchestrator* (or *Both*); on the rest, *Node* with the `DLLM1-…` code. The Linux wizard
only shows the firewall commands: run them with `sudo`. NVIDIA GPUs need the proprietary driver (CUDA ≥ 12.4) or,
otherwise, the Vulkan driver (`sudo apt install -y mesa-vulkan-drivers vulkan-tools`).

## 3. Install the orchestrator

Open the wizard (table in the README) and:

1. Choose **Orchestrator** or **Both**. Pick the language with the selector at the top (English by default).
2. Set the **DLLM folder** with **Browse…** (default `~/DLLM`): llama.cpp (`llama.cpp/`) and the models (`models/`) are
   installed there. Your system's folder picker opens in front of the browser (or, without a desktop, a browser inside
   the wizard that can also create folders). Below you see the disk's free space: a 7B model takes ~5 GB and a 32B one
   ~20 GB. The folder is created if it does not exist. The configuration and the cluster secret stay separately in
   `~/.dllm`, with restricted permissions.
3. Tick *Allow using the API from other computers* only if you will use it from outside the orchestrator itself: a
   mandatory **API key** will be generated.
4. Press **Install**. The wizard:
   - downloads the latest official llama.cpp version that publishes packages for every system (so any node finds its
     own of the same version) and verifies its **sha256**;
   - stores the configuration in `~/.dllm/config.json` (permissions 600, contains the cluster secret);
   - copies the program to `~/.dllm/dllm.pyz`;
   - registers the user **autostart** (no administrator needed);
   - on Windows, opens **only** the required ports in the firewall, limited to your network (asks for administrator
     permission once; see [Firewall](#firewall));
   - checks that the orchestrator responds.
5. Copy the **join code** `DLLM1-…` shown at the end. You can see it again in the panel (**Nodes → 🔑 Join code**, only
   on the orchestrator itself) or with `dllm join-code`.

> The join code contains the **cluster secret**: treat it like a password and do not paste it in public chats.

## 4. Install each node

1. Open the wizard and choose **Node**.
2. Paste the **join code**, or leave it empty to **claim it from the orchestrator** (see below). If the orchestrator is
   already running and they do not find each other on the network, enter the **orchestrator's IP**.
3. Choose **how much this computer shares**:
   - **Automatic** (recommended): managed by the orchestrator and changeable from its web panel.
   - **Limit from this computer**: percentage (e.g. 33 %) and, if you want, exact memory and threads. The orchestrator
     cannot change it.

   The wizard shows what each option means on your computer. More detail in
   [USAGE](USAGE.md#managing-what-each-node-contributes).
4. (Optional) *Advanced options*: computer name, backend.
5. **Install**. The node downloads **exactly the same llama.cpp build** as the orchestrator (it is in the code) and only
   accepts connections from the orchestrator's IP.

In a few seconds it appears in the orchestrator's panel (green indicator).

### Joining a node without a code (claiming it from the orchestrator)

If you install the node without a join code, it **waits** and announces itself on the local network (only its name and
port):

1. In the orchestrator's panel: **Nodes → Computers waiting to join → Claim**. A 6-digit **verification code** appears.
2. On the node (the last page of the wizard, or `dllm pair` / the *Open-DLLM* shortcut on that computer) the request
   appears with the **same code**. If it matches, press **Accept**. If the node **does not appear** (broadcast does not
   cross VLANs/VPNs): in the node's installer enter the **orchestrator's IP** (`--orch-ip`), or in the panel use
   **Nodes → Add by IP** / **Search the network**.
3. The orchestrator sends it the encrypted cluster secret and the node joins without restarting. From then on it only
   accepts connections from that orchestrator.

The code prevents another computer on the network from impersonating the orchestrator; see
[SECURITY](SECURITY.md#pairing-without-a-code). The request expires after 5 minutes. With a join code none of this is
needed.

## 5. First model

1. Add a model from the panel (*Add models*: Hugging Face, Ollama or local Ollama, see
   [USAGE](USAGE.md#models-add-manage-and-tune)) or copy a `.gguf` to the orchestrator's models folder.
2. In the panel: choose the model → **Simulate** (measures the network and shows what goes to each node) → **Load**.
3. When the state is **READY**, try the panel chat or the API. See [USAGE](USAGE.md).

For a first test use a small model (e.g. 7–8B Q4_K_M, ~5 GB) and compare it with a single computer.

## Choosing a backend (CPU / GPU)

The wizard picks the **fastest available** for the hardware (you can change it in *Advanced options*, where each option
explains what it is for). With *auto*, if that backend cannot be installed or does not start (old driver, missing
runtime), it **tries the next one** (CUDA → Vulkan → CPU) and says so. Then the agent **detects its GPU on its own** by
asking llama.cpp, lends it with its real VRAM (also AMD and Intel) and, if the computer has several, uses the one with
most memory (the integrated one is discarded if there is a dedicated one). The wizard shows the detected GPU at the end.

| System | Hardware | Suggested backend | Alternatives |
|---|---|---|---|
| Windows / Linux (x64 and ARM64) | NVIDIA, driver with CUDA ≥ 12.4 | `cuda` (12.x or 13.x depending on the driver, with its runtime) | `vulkan`, `cpu` |
| Windows / Linux | AMD | `vulkan` | `rocm` (experimental, needs ROCm/HIP), `cpu` |
| Windows / Linux | Intel (Arc, iGPU) | `vulkan` | `sycl` (experimental, needs oneAPI), `cpu` |
| Windows / Linux | NVIDIA with an old driver | `vulkan` | `cpu` |
| Windows / Linux | No GPU | `cpu` | |
| macOS Apple Silicon | — | `metal` | Unified memory counts as VRAM |
| macOS Intel | — | `cpu` | |

All computers use the same version (`bNNNN`) even if each one has a different backend: a Linux node with CUDA and a
Windows one with Vulkan work together.

**Windows 11 with Smart App Control:** it blocks llama.cpp's unsigned DLLs (error `0xc0e90002` and Defender warning
"Part of this app has been blocked"). Turn it off in Windows Security → App & browser control before installing; the
wizard detects and explains it. If Windows answers "`[WinError 4551]` An Application Control policy has blocked this
file", it is the same: the wizard **stops** with that explanation instead of trying another backend (CPU uses the same
DLLs and would also be blocked).

**CUDA on Linux** is downloaded prebuilt (official `ubuntu-cuda` package, with its runtime); you only need the NVIDIA
driver (`nvidia-smi` must work; in a VM, with the GPU in *passthrough*). To build it yourself:
`LLAMA_REF=b1234 ./build-llamacpp.sh cuda` (same tag as the orchestrator) and *Use an already installed llama.cpp*.

**Several GPUs in one computer:** one is lent (the one with most memory). To choose another: `dllm agent --device CUDA1`,
or `"device"` in the agent configuration.

**Example: two Windows PCs with an RTX 3060 (12 GB) and an RX 7600 XT (16 GB).** Install the orchestrator on one of them
with the **Both** role (or on a third computer as *Orchestrator*) and the other as **Node**. The NVIDIA one uses CUDA and
the AMD one Vulkan, detected on their own. Together they contribute ~24 GiB of VRAM: enough for a 32B in Q4_K_M with ~8k
context or a 27B with more. If the model fits in a single GPU, DLLM uses only that one (directly, without RPC, when it is
the orchestrator's own GPU). Connect both by cable.

## Unattended installation (no questions)

```bash
# orchestrator that is also a node, API reachable from the network
python dllm.pyz setup --tui --yes --role both --models-dir ~/models --expose
# node
python dllm.pyz setup --tui --yes --role node --join DLLM1-xxxxxxxx
```

Useful `setup` options: `--backend cpu|vulkan|cuda|rocm|metal`, `--bin-dir FOLDER`, `--name NAME`, node contribution
(`--share 33`, `--mem-gb N`, `--threads N`, `--limits-mode auto|manual`; if you give a limit without `--limits-mode`, it
becomes manual), `--no-service`, ports (`--api-port`, `--agent-port`, `--rpc-port`, `--beacon-port`), `--dry-run`
(simulates without writing anything). Full list: `python dllm.pyz setup --help`. Language: `DLLM_LANG=es` or
`--lang es` before the subcommand.

## Offline

1. On a computer with Internet download the llama.cpp package from its releases page (the same tag for everyone) and
   unzip it, or build it with `build-llamacpp.sh`.
2. Copy it to each computer and in the wizard give that folder in *Use an already installed llama.cpp* (`--bin-dir`).
   It must contain `llama-server` (orchestrator) and/or `ggml-rpc-server` (nodes; `rpc-server` in old llama.cpp builds
   also works).

## Opening the panel

The wizard creates a **DLLM** shortcut (desktop and Start menu on Windows; applications menu on Linux) that runs
`dllm open`: it starts DLLM if stopped, opens the panel in the browser and **hides the console**. If you close the
browser with DLLM running, the console reappears and offers **[A] open again**, **[P] stop DLLM** or exit leaving it
running. The package also has `Open-DLLM.bat`.

**No password on the computer itself:** the panel asks for nothing from the orchestrator. If you enabled *Allow using
the API from other computers*, the API key is needed from them (it is the project's security rule: nothing listens on
the network without authentication), but you do not need to type it: at the end the wizard gives a **link for other
computers** (`http://IP:8080/#key=…`) that you open once and the browser remembers it. The panel's *API key* button
gives it too, when opened on the orchestrator.

## Firewall

**Windows:** the wizard creates the rules itself (*Open only the required ports…* checkbox, enabled by default; disable
it or use `--no-firewall` to do it manually). Windows asks for administrator permission **once**. The rules are called
`DLLM-agente`, `DLLM-beacon` and `DLLM-API`, they are limited to the Python executable DLLM uses, to the given ports and
to the orchestrator's IP (node) or the local subnet (orchestrator), and are deleted on uninstall. If permission is
denied, the wizard shows the equivalent commands. **Linux and macOS:** nothing is touched (needs `sudo`); the exact
commands are shown at the end.

In the *Both* role the local agent listens only on `127.0.0.1`, so it needs no rules. Default ports:

| Computer | Port | Purpose | Open to |
|---|---|---|---|
| Node | TCP 8765 | Agent control API | Only the orchestrator's IP |
| Node | TCP 50051 | Model data (authenticated RPC proxy) | Only the orchestrator's IP |
| Orchestrator | UDP 50999 | Node discovery | The nodes' subnet |
| Orchestrator | TCP 8080 | Panel and API | Only if you use `--expose`, and to whoever uses it |

Linux example (node, orchestrator at 192.168.1.10):
```bash
sudo ufw allow from 192.168.1.10 to any port 8765 proto tcp
sudo ufw allow from 192.168.1.10 to any port 50051 proto tcp
```
On macOS, if the *allow incoming connections* prompt appears for Python, allow it. On Windows, with the DLLM rules
created the Defender warning should not appear; if you accepted it with an earlier version, Windows created broad rules
for `python.exe`/`pythonw.exe` (all ports): you can delete them in *Windows Defender Firewall → Advanced settings →
Inbound rules* and keep only the `DLLM-*` ones.

## Where it is installed

| What | Path |
|---|---|
| Configuration (secret included) | `~/.dllm/config.json` |
| Program | `~/.dllm/dllm.pyz` |
| Downloaded llama.cpp | `<DLLM folder>/llama.cpp/llama-<tag>-<backend>/` |
| Models (orchestrator) | `<DLLM folder>/models/` (or the models folder you already had) |
| Automatic safety backups before restoring | `~/.dllm/backups/` (keeps 5) |
| Firewall rules (Windows) | `DLLM-agente`, `DLLM-beacon`, `DLLM-API` |
| Linux service | `~/.config/systemd/user/dllm.service` (`systemctl --user status dllm`) |
| macOS service | `~/Library/LaunchAgents/com.dllm.plist` (log in `~/.dllm/dllm.log`) |
| Windows service | Scheduled task `DLLM` (at logon) |

On Windows `~` is `%USERPROFILE%`. To start on Linux without logging in: `sudo loginctl enable-linger $USER`.

## Updating

Get the new package (or run the one-command installer again) and run the wizard **on the orchestrator**. It keeps its
secret, so nodes do not need a new code. If the llama.cpp version changes, **nodes update themselves**: the orchestrator
asks them to install the same one (official package, sha256 verified). To also update the DLLM program on a node, run
the wizard on it.

## Restore from a backup (disaster recovery)

On the new computer, install with the installer, choose **Orchestrator** or **Both** and open *"Restore from a backup"*
in the first step: load the `.dllmbak` file (and its password). The cluster secret (nodes reconnect on their own), the API
key, model and node settings and the nodes added by IP are recovered; name and paths are those of the new machine.
Models do not travel in the backup: the panel (System) downloads them again. See
[USAGE](USAGE.md#backup-and-recovery).

## Uninstalling

The uninstaller is a **separate program** from the installer and works the same for a node, an orchestrator or both:

- **From the package:** double-click `Uninstall-DLLM.bat` (Windows) or `Uninstall-DLLM.command` (macOS), or
  `./uninstall.sh` (Linux). It opens a page in the browser (in a terminal without a desktop, it asks by text).
- It shows what is installed and three levels: **only stop** (reversible), **remove DLLM and its configuration** (models
  are kept) or **remove everything** (also the models, only if DLLM created them). "See exactly what will be done" lists
  each step.
- On an orchestrator it offers to **first save a backup of the whole configuration** (encryptable with a password)
  outside the folder that will be deleted; if the backup fails nothing is uninstalled.
- **Command line** (`dllm uninstall --gui` opens the page; `--interactive`, questions in the terminal):

```bash
dllm uninstall                   # stops the program, removes autostart and firewall rules
dllm uninstall --purge           # also deletes llama.cpp and ~/.dllm (configuration and secret)
dllm uninstall --purge --models  # also deletes the models, only if they are in <DLLM folder>/models
```

**Uninstalling is thorough:** besides the scheduled task and the firewall rules, it **closes every DLLM process** (agent,
orchestrator, `llama-server`, `rpc-server`), repeating until none is left, and if one cannot be closed it says so with
its PID. With `--purge` it deletes folders even with read-only files (retrying if something uses them for a moment). It
also cleans leftovers from the **previous version of the program**; its models are not touched. If something could not
be deleted, the result marks it with "NOT".

Models are kept unless you ask, and a models folder DLLM did not create is never deleted.

Where `dllm` is `python ~/.dllm/dllm.pyz` (Windows: `py -3 %USERPROFILE%\.dllm\dllm.pyz`).
