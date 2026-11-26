# Troubleshooting

**English** · [Español](es/SOLUCION-PROBLEMAS.md)

First, on the affected computer:

```bash
dllm doctor        # Python, configuration, llama.cpp binaries, same build, program running
dllm status        # (on the orchestrator) nodes, state, plan and why each node is excluded
```

Logs: Linux `journalctl --user -u dllm -f` · macOS `~/.dllm/dllm.log` · Windows: run `dllm run` in a console to see it
live (the scheduled task runs without a window). In the panel, **Logs** shows the last lines of `llama-server` when a
load fails.

## The model restarts (RECOVERING) when sending an image

Look at *Logs*: after "llama-server exited (code N)" the failure lines appear. If you see
`GGML_ASSERT … non-causal attention requires n_ubatch >= n_tokens`, the physical batch is smaller than the image tokens
(Gemma 3/4 process the image with non-causal attention in a single batch; Gemma 4 accepts up to 1120 tokens per image).
Fix: in *Models → Parameters* raise **Logical batch (batch)** and **Physical batch (ubatch)** (e.g. 2048; the physical
one can never exceed the logical one). A larger batch uses more compute VRAM: lower the context if needed. Do not resend
the image until the model is reloaded with the change.

If instead you see `ggml-rpc.cpp … Remote RPC server crashed or returned malformed response` or `recv returned 0 (peer
closed?)`, what died is the **node's `rpc-server`** (that is why `llama-server` exits with a code like `3221226505`, an
abort): it is not the batch. Since 0.6.0 *Logs* adds below "rpc-server of <node> exited (code N)" with its last lines
(Vulkan/CUDA driver failure, GPU out of memory…), and `/api/state` includes them in `last_crash.rpc`; the agent also keeps
them in its log and in `rpc_last_exit` of `/info`. With that you can tell a GPU driver failure from a lack of compute
memory (in that case try a smaller context).

## Installation

| Symptom | Likely cause | Fix |
|---|---|---|
| "DLLM needs Python 3.9…" | Python missing or old | Accept installing it or download it from python.org (on Windows tick *Add to PATH*) |
| macOS: "cannot be opened because it is from an unidentified developer" | Gatekeeper | Right-click → Open, or `xattr -dr com.apple.quarantine FOLDER` |
| "could not query llama.cpp releases" | No Internet, proxy or GitHub API limit (60/h per IP) | Try again later or use `--bin-dir` with llama.cpp downloaded manually |
| "… version bNNNN is set by the orchestrator … that package was not published" | The orchestrator was installed with an incomplete llama.cpp version (DLLM ≤ 0.3.0) | Update DLLM on the orchestrator, run its wizard again and use the new join code on the nodes (already installed ones update themselves) |
| "there is no llama.cpp package for …" | System/backend combination without an official binary | Another backend or build with `build-llamacpp.sh` + `--bin-dir` |
| "the downloaded package does not contain llama-server and rpc-server" | DLLM < 0.3.3: llama.cpp renamed `rpc-server` to `ggml-rpc-server` | Update DLLM. If it persists, that build does not include RPC: build with `build-llamacpp.sh` (enables `GGML_RPC=ON`) |
| An NVIDIA GPU (e.g. in a Proxmox VM with *passthrough*) is installed with Vulkan instead of CUDA | The installer only picks CUDA if `nvidia-smi` works and its driver supports CUDA ≥ 12.4; virtualization does not prevent it if the guest driver is recent. The backend is set at install time and the panel does not change it | On the node: `nvidia-smi` (must show "CUDA Version: 12.4" or higher); update the driver and run the wizard again with `--backend cuda` (`py -3 dllm.pyz setup --backend cuda`). The wizard explains the reason |
| When claiming a node: "Could not claim: this node is already paired" | Another process (an agent from a previous installation not closed on uninstall) holds the agent port and answers instead of the new one | On the node: Task Manager → close old `pythonw.exe`/`python.exe` (or restart the computer) and start DLLM again; check with `netstat -ano \| findstr :<port>`. Since 0.6.0 the agent refuses to start if the port is busy and explains why |
| Windows: "`[WinError 4551]` An Application Control policy has blocked this file" when installing, "llama-server-impl.dll is not designed to run on Windows… 0xc0e90002", Defender warning "Part of this app has been blocked", or "llama-server does not start … timed out" | Windows 11 **Smart App Control** blocks unsigned DLLs; the official llama.cpp binaries are not signed | Windows Security → App & browser control → Smart App Control settings → **Off**, and repeat the installation. Since the April 2026 update it can be turned on again without reinstalling Windows. There are no per-program exceptions. `dllm doctor` warns if it is on |
| Defender warning "Windows Firewall has blocked some features of Python" | DLLM ≤ 0.3.5 did not create firewall rules | Since 0.3.6 the wizard creates limited `DLLM-*` rules. If you already accepted the warning, you can delete the broad `python.exe`/`pythonw.exe` rules (see [Firewall](INSTALLATION.md#firewall)) |
| AMD/Intel GPU on Windows with no usage or VRAM in the monitor | DLLM ≤ 0.3.6 did not read Windows counters correctly | Update. Temperature, power and fan are still not available on Windows for AMD/Intel |
| "the sha256 … does not match" | Corrupted or tampered download | Try again; if it persists, check proxy/antivirus. **Do not** ignore it |
| "rpc-server does not start … libvulkan" | Vulkan runtime missing | `sudo apt install libvulkan1 mesa-vulkan-drivers` or the vendor driver, or the `cpu` backend |
| "incomplete or corrupted code" | The code was cut when copying | Copy it again in full (Nodes → 🔑 Join code or `dllm join-code`); spaces and line breaks are tolerated |
| *linger* warning on Linux | The service only runs with the session open | `sudo loginctl enable-linger $USER` |
| Autostart not installed | No user systemd (container, WSL…) | Start with `dllm run` (e.g. in `tmux` or a system service) |
| The one-command installer fails on Windows | PowerShell execution policy, proxy or Python from the Microsoft Store | Run it in a normal PowerShell window; if Python was just installed, open a new window and repeat. The `install.ps1` script has not been tested on a real Windows yet: report the output |

## Nodes do not appear in the panel

1. Is the agent running on the node? `dllm doctor` there.
2. Does discovery arrive? The node sends UDP 50999 to the orchestrator: open that port on the **orchestrator**.
3. Different subnets or VPN? Reinstall the orchestrator with `--advertise-host REACHABLE-IP` and the nodes with the new
   code, or add them with **Nodes → Add by IP**.
4. It shows **red** with "HTTP 403"/"invalid signature": the node has another secret or the orchestrator's IP changed
   (the node only accepts the IP in the code). Reinstall the node with the current code.
5. Intermittent "invalid signature": clocks out of sync by more than 60 s. Enable NTP on all computers.
6. A node installed **without a code** that does not appear in *Computers waiting to join*: announcements go by
   broadcast to UDP port 50999 and do not cross subnets or VPNs; use the join code. If claiming it "expires", confirm on
   the node within 5 min (last page of the wizard or `dllm pair`). If the code does not match, **reject**: another
   computer on the network tried to claim it. Alternatives without broadcast: in the panel, **Nodes → Add by IP** or
   **Search the network**; when installing the node, give the orchestrator's IP (`--orch-ip`).

## A node has a GPU but Monitor says "no GPU" (warning "llama.cpp does not use it")

`nvidia-smi` sees the GPU, but the installed `rpc-server` is CPU-only (or the driver/Vulkan was missing when it
started), so it cannot lend it: the node announces itself as CPU/RAM and shows the warning. On the node, run the wizard
again choosing CUDA or Vulkan, or install the driver; the agent re-detects devices every minute, without restarting.
Check with `rpc-server --list-devices` and `python3 ~/.dllm/dllm.pyz doctor`.

## A node is excluded from the split

`dllm plan MODEL` gives the reason:

| Reason | Fix |
|---|---|
| `network X Mbps < minimum 800 Mbps` | Gigabit cable/switch; check negotiation (`ethtool eth0`, should say 1000Mb/s). Last resort: `--min-mbps`, or *Nodes → Use anyway* for that node (forced mode, asks for confirmation). If you change the cable, repeat *Measure the network* |
| `different build (…); it will be asked to update` | Normal: the node installs the orchestrator's build by itself in a few minutes ("updating llama.cpp … %") |
| `different build and the update failed: …` | No Internet on the node, or no package for its system/backend: install that build manually with `--bin-dir`, or reinstall the node with the current code |
| `rpc-server not installed` | Reinstall the node |
| `not enough free memory` | The node contributes very little: raise it with **Configure** in the panel (automatic node) or on the node itself (manual) |
| `no response` | See the previous section |

## The node does not lend the GPU I expected

- The panel shows in *Device* what it detected. If it says "CPU" on a computer with a GPU: the installed llama.cpp
  package has no GPU backend (reinstall the node choosing `cuda`/`vulkan`) or drivers are missing (Vulkan on AMD/Intel,
  recent NVIDIA driver for CUDA).
- With several GPUs the one with most memory is used; for another, `--device` (e.g. `CUDA1` or `7600`).
- Manual check on the node: `ggml-rpc-server -d x` (`rpc-server` in old builds) lists the devices llama.cpp sees.

## The monitor shows "—"

- It is data that system does not offer without extra software: on Windows, the temperature and power of AMD or Intel
  GPUs; on macOS, power and temperature. Usage and VRAM should be visible.
- NVIDIA without data: check that `nvidia-smi` works on the node (it comes with the driver).
- AMD on Linux without power/temperature: old kernel or driver; check that `/sys/class/drm/card*/device/hwmon` exists.
- No data at all from the node: the agent is from an older version; reinstall it with the wizard.

## Loading fails

| Message | Fix |
|---|---|
| `does not fit: I need X GiB … contribute Y GiB` | More nodes, nodes contributing more (**Configure**), a more quantized model or a smaller `--ctx` (the message gives the maximum context that fits) |
| The panel says "the node sets its limits locally" | That node is in manual mode: change it on the node itself with the wizard |
| `llama-server exited: … failed to allocate` | The estimate fell short: lower `--ctx` or the memory offered by some node |
| `llama-server was not ready in 1800 s` | Huge model over a slow network; raise `--load-timeout` |
| `the pinned layers (X) exceed the model's` | Check **Fixed layers** in *Nodes → Configure* (empty = automatic) |
| `the quantized V cache requires flash attention` | In the model parameters, set flash attention to `auto` or `on`, or the V cache to `f16` |
| `local_gpu=on: cannot run directly on the local GPU (…)` | The model is forced to the local GPU but the plan does not allow it: set *Direct local GPU* to automatic or pick the local GPU as target |
| `tunnel → … failed` in events | The agent does not accept the connection: secret, `allow_ip` or firewall on port 50051 |
| Absurd split (everything to a slow node) | The bandwidth estimates do not match your hardware: set `--bw-gbps` per node |

## Model downloads

| Message | Fix |
|---|---|
| `HTTP 401/403 … gated access` | Accept the model's terms on its Hugging Face page and save your token in the panel |
| `could not connect to huggingface.co / registry.ollama.ai` | The orchestrator has no Internet access or there is a proxy: download the `.gguf` on another computer and copy it to the models folder |
| `not enough space: needed X GiB` | Free space or change the models folder (orchestrator wizard) |
| `the sha256 does not match` · `is not a GGUF file` | Damaged or tampered download: discarded automatically; try again |
| `incomplete download` | The connection dropped: press download again and it resumes |
| An Ollama model does not load (`llama-server exited: … unknown model architecture` or similar) | That model uses Ollama's own format: download the GGUF version from Hugging Face |
| **📎 Attach** does not appear in the chat | The model has no projector: download it from its repo (`mmproj-*.gguf`) into the same folder, or pick one in *Parameters*, and reload the model |

## It is very slow

- Look at the **Mbps and ms** of each node in the panel: > 1 ms of RTT already costs. Compare with `ping`.
- Use fewer nodes: if the model fits in two, the third one only subtracts.
- A node with a weak CPU/slow memory limits everyone: remove it and reload.
- Compare with the same model on a single computer to know how much the split costs.

## Starting from scratch

```bash
dllm uninstall --purge     # on every computer
```
and install again starting with the orchestrator.
