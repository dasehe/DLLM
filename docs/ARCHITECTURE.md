# Architecture

**English** · [Español](es/ARQUITECTURA.md)

## Components

```
┌────────────────────────── ORCHESTRATOR ──────────────────────────┐        ┌──────────── NODE ─────────────┐
│ HTTP :8080  panel · /api · /v1 (proxy) · /metrics                 │        │ HTTP :8765  HMAC-signed API    │
│   │                                                               │        │   /info /rpc/* /limits         │
│   ├─ Registry  signed UDP beacons :50999 + /info polling (3 s) ───┼──────► │   /bench/*                     │
│   ├─ Scheduler reads GGUF, measures network, decides the split    │        │                                │
│   ├─ Cluster   lifecycle, watchdog and recovery                   │        │ TCP :50051  RPC proxy          │
│   │     │                                                         │        │   mutual HMAC handshake        │
│   │     └─ llama-server (127.0.0.1) --rpc 127.0.0.1:<tunnel i>    │        │        │                       │
│   │                        │                                      │        │        ▼                       │
│   └─ Tunnel i ◄────────────┘  HMAC handshake ─────────────────────┼──────► │ rpc-server (127.0.0.1 only)    │
└───────────────────────────────────────────────────────────────────┘        └────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `dllm/agent.py` | Node agent: starts/watches `rpc-server`, authenticated proxy, beacons, benchmark, limits |
| `dllm/orchestrator.py` | Node registry, planning, `llama-server` lifecycle, API, metrics |
| `dllm/scheduler.py` | Cost model, split and maximum context |
| `dllm/models.py` | Model catalog, downloads (Hugging Face, Ollama, local Ollama), multimodal projectors and per-model parameters |
| `dllm/telemetry.py` | Node telemetry: GPU (nvidia-smi, amdgpu sysfs, Windows WDDM counters, ioreg), CPU, RAM, RPC traffic |
| `dllm/limits.py` | How much each node contributes (memory, threads, auto/manual mode) |
| `dllm/gguf.py` | GGUF metadata reading (layers, heads, context, size, split models, chat template) |
| `dllm/hw.py` | Memory, NVIDIA GPU, memory bandwidth, binary lookup |
| `dllm/engine.py` · `ledger.py` | Engine drivers, routing by capability, memory ledger per slot and node |
| `dllm/auth.py` · `util.py` | HMAC signatures, handshake, TCP tunnel |
| `dllm/installer.py` · `wizard.py` · `config.py` | Installer, wizard/uninstaller, configuration and join code |
| `dllm/backup.py` | Full configuration backup and restore |
| `dllm/i18n/` | Languages: English by default, Spanish (Spain) optional |
| `dllm/ui.html` · `setup.html` · `uninstall.html` | Panel, web wizard and uninstaller (static HTML, no dependencies) |

Python standard library only; `llama.cpp` does the compute.

## What travels over the network (and what does not)

The model is not chopped into "tasks" sent over and over. It works like this:

1. **When loading**, the orchestrator reads the `.gguf` and sends each node **its layers** (the weights). This happens
   once and is the slow part: a 30 GB model at 1 Gbps takes 4–5 minutes.
2. **Each node keeps its layers in its RAM/VRAM and also the context cache (KV) of those layers.**
3. **For each token**, only the activation vector travels from one layer to the next (a few KB): orchestrator → node 1 →
   node 2 → … → orchestrator. Each node computes its layers with its local data.

Consequences:

- **More memory in the cluster = more possible context.** The KV cache grows with the context and is split among the
  nodes together with their layers. In models with **sliding window attention** (Gemma 2/3/4, Cohere 2…) the SWA layers
  only keep the window, so the context fits in much less memory; the calculation takes it into account (per-layer
  pattern, KV heads and per-layer dimensions, layers with a shared cache), like llama.cpp. The split shows the **maximum
  context** that fits with the nodes' memory (`max_ctx`), capped at the model's training context: going beyond it
  degrades quality.
- **A fast orchestrator barely changes speed.** What matters is the network latency per hop (each token goes through
  every node) and each node's memory bandwidth.
- **More context is not free.** Processing a long prompt costs compute, especially on CPU, and each token reads the whole
  KV cache, so with large contexts each answer takes longer.
- "Remembering" here means **working memory within a conversation**, not memory across conversations.

## Agent self-configuration

The goal is that installing the agent is enough. At startup, after every update and every minute while it has no
accelerator:

1. **It detects the devices by asking llama.cpp:** `rpc-server -d <nonexistent>` lists what it sees with its memory
   (`Vulkan0: AMD Radeon RX 7600 XT (16368 MiB, 15800 MiB free)`). It works for NVIDIA (CUDA), AMD/Intel (Vulkan, ROCm)
   and Apple (Metal). Module `dllm/devices.py`.
2. **It picks a device:** the accelerator with most memory, or the one given with `--device` (name or part of the
   description). It fixes it with `rpc-server -d`, because by default rpc-server exposes **all** accelerators. The
   Vulkan backend already discards the integrated GPU if there is a dedicated one.
3. **It reports its real memory** (total and free VRAM, measured again every minute while idle). Without an accelerator
   it lends CPU and RAM; if `nvidia-smi` sees a GPU that llama.cpp does not offer, it announces itself as CPU/RAM with a
   warning (`gpu_warning`) instead of counting VRAM it cannot use.

**Same version across the cluster:** the RPC protocol requires the same llama.cpp build. If the orchestrator sees a node
with another build, it asks it (`POST /llama/install`, signed) to install its own. The agent downloads the official
package for its system and backend (the installed one, `--llama-backend`), verifies the sha256, switches binaries,
re-detects devices and saves it in its configuration. At most one attempt every 15 min per node. Disabled with
`--no-auto-update` on the node or `--no-auto-sync` on the orchestrator.

**Devices on the orchestrator:** before launching `llama-server` its devices are listed without and with `--rpc …`. The
difference is the remote ones, in order, and it is launched with `--device <only those>` and an aligned `-ts`. So the
orchestrator's local GPU does not enter the RPC split (*Both* role on a PC with a GPU), and it does not depend on how
llama.cpp names RPC devices. If a node exposed more than one, the load fails with a clear message. It also warns if a
node is assigned more than its device has free.

## How much each node contributes

Module `dllm/limits.py`. Each node offers **memory** (RAM, or VRAM if it uses an NVIDIA GPU) and **CPU threads**.

| Mode (chosen when installing the node) | Who decides | Orchestrator panel |
|---|---|---|
| **Automatic** (default) | The orchestrator. Starts with the recommended value | **Configure** button on the node |
| **Manual** | The node itself (wizard or `--share/--mem-gb/--threads`) | Read-only ("set on the node") |

Values (priority: exact value > percentage > recommended):

- **Recommended:** memory = the minimum of (free − reserve) and 75 % of the total in RAM or 95 % in VRAM; threads = half
  of the logical threads (≈ physical cores, what performs best in llama.cpp).
- **Percentage (`share`):** applied to total memory and threads. Example: 16 GiB and 6C/12T at 33 % → ~5.3 GiB and 4
  threads.
- **Cap:** never more than the total minus the reserve (1 GiB by default) nor more threads than the computer has. If
  more is requested, it is clamped and the panel says so.

How they are enforced: **threads** are passed to `rpc-server -t N`, a real CPU limit. **Memory** has no option in
`rpc-server`, so the planner respects it: it never assigns a node more layers and KV cache than it offers. A GPU can only
be limited by memory, not by % of compute. The estimated bandwidth of a CPU node drops proportionally if it is given
fewer threads than its physical cores.

Changes made from the panel are saved in the node's `config.json` and survive restarts. If the node is in use, the new
memory counts in the next split and threads when the model is reloaded.

## Load flow

1. `POST /api/load` → the worker takes the turn (one load at a time; a new load cancels the previous one).
2. It reads the GGUF and computes the needed memory: weights + KV cache (per layer with its own cache:
   `cells·n_head_kv·(dim_k+dim_v)·bytes`, with cells = context in global layers and window + 512 in SWA layers) + 5 %.
3. It measures **sequentially** each live node: RTT (minimum of 5 pings) and 16 MiB up and down. In parallel they would
   share the orchestrator's link and all would look slower.
4. It excludes nodes: no response, no `rpc-server`, **different llama.cpp build** (the RPC protocol is not compatible
   across versions), network < `min_mbps` (unless forced), not enough memory.
5. Scheduler → chosen nodes, `--tensor-split` and tok/s estimate.
6. If the whole model goes to a single GPU of this same computer, it is launched directly (see below). Otherwise it asks
   each node for `/rpc/start`, opens a local tunnel per node and launches
   `llama-server -m MODEL --rpc 127.0.0.1:T1,127.0.0.1:T2… -ts s1,s2… -ngl 999 -c CTX`.
7. It waits for `llama-server`'s `/health` → `READY`.

## Scheduler

Layer-pipelined inference, one stream, memory-bound:

```
t_token ≈ Σ_i (bytes_i / (bw_i · 0.65))  +  Σ_i rtt_i  +  3 ms
```

Minimizing that sum with `Σ bytes_i = M` and `bytes_i ≤ capacity_i` is solved by **filling first the nodes with the
highest memory bandwidth** and using **the minimum number of nodes** (each extra node adds latency). Layers are assigned
proportionally with the largest remainder method. Margin per node: 512 MiB + 5 % free.

**GPU + RAM of the same node (0.17):** a GPU node whose `rpc-server` exposes several devices offers two entries to the
planner: its VRAM and its RAM (`host` = the same computer, RAM bandwidth ~20 GB/s unless set). Both entries share the
tunnel and count as **one network hop**, and they are kept together and in device order (GPU, then CPU) because the
node starts a single `rpc-server -d <GPU>,CPU`; llama-server sees two remote devices and `--tensor-split` gives each its
share. Order of preference between splits that fit (0.17.1): (1) the **priority** chosen for the nodes (bytes on
prioritized nodes), (2) no tier-3 links if possible, (3) **as much as possible on GPU**: GPU1 + GPU2 + … before RAM1 +
RAM2 + … (VRAM is always much faster), (4) the highest tier used, (5) remote computers (hops), (6) estimated time per
token, (7) number of computers. So, with no priority set, all the cluster's GPUs are filled first (also in large
farms: with more than 12 entries the planner fills greedily in that same order) and only what does not fit goes to RAM,
using first the RAM of computers already in the split.

### Model slots and memory ledger

Each loaded model takes a *slot* (0-7). The slot is a `Cluster` with its `llama-server` (ephemeral internal port on
127.0.0.1) and, on each node it uses, its own `rpc-server` (ephemeral local port) behind the agent's same authenticated
proxy: the client gives the slot in the HMAC handshake label (`c` + number; slot 0 uses `c`, as in 0.4.x), so each slot
has its independent channel without opening new ports. `ledger.Ledger` keeps the reservations per slot and node:
available = contribution − other slots' reservations − margin (256 MiB if there is already another slot on the node),
and "plan + reserve" is done under a common lock. An agent older than 0.5.0 does not support slots > 0 (it is excluded).

### Device policy, memory and queue

`device_policy` (model setting): `auto` (default: all nodes, `-ngl 999`), `gpu_strict`, `cpu_only` and
`gpu_with_cpu_fallback`. With `cpu_only` and with the CPU part of the *fallback*, llama-server uses the orchestrator
computer's CPU and memory is reserved in `@host` (real free RAM − margin − reservations). The ledger has two dimensions
(`vram`, `ram`) and configurable margins. A slot is `IDLE` or `BUSY` (`active_requests > 0`); LRU only touches unpinned
`IDLE` slots; if all candidates are `BUSY`, the request waits in a queue bounded by `--queue-timeout` and then receives
`429` + `Retry-After`; a model that does not fit even without the unpinned ones receives `503` before launching anything.
On a CPU-only node what is reserved is RAM; on one with a GPU, VRAM plus 256 MiB of RAM for I/O buffers.

### Engines, L7 routing and on-demand

`engine.EngineDriver` decouples the slot from the process: capabilities, endpoints, command, health check, memory
estimate and default placement (`split` across nodes over RPC, or `node`, pinned to a single node and GPU: *Task
Affinity*, e.g. embeddings). The `/v1/*` proxy looks at the `model` field, finds the slot whose model matches and checks
that its driver supports the route's capability (`engine.OPENAI_ROUTES`); if nothing matches, the main model answers.
With `--on-demand`, a `.gguf` in the models folder is loaded into a new slot (synchronized with a lock, with a prior plan
probe) and, if memory is short, `pinned: false` slots that are ready and without requests in progress are evicted by LRU
until it fits. The proxy does not buffer: SSE and binary responses are forwarded as is. The request that triggers the
load waits until it finishes (it may take minutes; set the client's timeout).

### Network topology (tiers)

The orchestrator measures each link before planning (`util.probe_link`: 1-byte echoes for RTT and 4 MiB blocks in each
direction, over the agent's proxy port and with the same mutual HMAC handshake as the RPC tunnel, but in "probe" mode,
which does not touch `rpc-server`) and classifies it (`scheduler.classify`):

| Tier | Link | Criterion |
|---|---|---|
| 0 | same computer | node host = loopback or own IP |
| 1 | fast LAN | RTT < 1 ms and ≥ 2000 Mbps measured |
| 2 | LAN | the rest with RTT ≤ 10 ms (includes 1 GbE and the 1–10 ms gap) |
| 3 | Wi-Fi / VPN / WAN | RTT > 10 ms |

Among the splits that fit (exhaustive search up to 12 nodes; greedy above) the choice is: **lowest maximum tier → fewer
remote nodes → lower time per token → fewer nodes**. Nodes are filled by ascending tier, so the farthest one holds fewer
layers. A tier 3 node only enters if the model does not fit without it (and the plan warns). The thresholds are
constants in `scheduler.py` (`TIER1_MBPS`, `TIER1_RTT_MS`, `TIER3_RTT_MS`). Limitation: bandwidth is measured with a
single TCP stream in Python; a 2.5 GbE link may fall below `TIER1_MBPS` and count as tier 2.

Node `bw`: NVIDIA GPU 300 GB/s by default; CPU measured with a single-thread `memcpy` × min(cores, 2). These are
**heuristics for ranking**, not a benchmark: they can be set with `--bw-gbps`.

## Node discovery and life

- The agent sends every 3 s a signed UDP beacon (`name`, ports, time) to `announce_to` (the orchestrator, taken from the
  join code or `--orch-ip`) or, if none, to broadcast.
- The orchestrator polls each node's `/info` every 3 s; a node without response for 10 s is considered down; one
  discovered by beacon that has been silent for 10 min is forgotten. Nodes added by IP are stored in `nodes_added.json`
  and reloaded at startup.
- Cluster watchdog (1 s): a node in use down or `llama-server` dead → `RECOVERING` and replanning.
- The agent restarts `rpc-server` if it dies while active.
- **Node without a join code**: announces `DLLMPAIR1` (unsigned) by broadcast; the orchestrator lists it in `pending`
  (30 s lifetime) and, when claimed, runs the pairing in `pairing.py` (DH, verification code accepted on the node,
  encrypted secret). The agent switches to normal mode on the fly and stores the secret in its `config.json`.
- All HTTP responses from the agent and the orchestrator use `TCP_NODELAY`: without it, headers and body went out in two
  sends and the second waited for the other computer's delayed ACK (~40 ms of RTT measured between computers).

## Models and downloads

- **Hugging Face:** `GET /api/models?search=…&filter=gguf`, file tree `GET /api/models/{repo}/tree/main` (sha256 in
  `lfs.oid`), download from `/{repo}/resolve/main/{file}`.
- **Ollama:** manifest `registry.ollama.ai/v2/{ns}/{model}/manifests/{tag}`. The `application/vnd.ollama.image.model`
  layer is the GGUF (its digest is the sha256); `…params`, `…system` and `…projector` are read too. **Local Ollama:**
  `~/.ollama/models/manifests/…` and `blobs/sha256-…` (or `OLLAMA_MODELS`).
- **Download queue:** one at a time, in a `.part` file, resumable with `Range`, free space check (file + 1 GiB), sha256
  and GGUF header before accepting it. The history of finished downloads can be cleared.
- **Per-model parameters:** `~/.dllm/models.json` on the orchestrator. Injected into every `/v1/chat/completions` and
  `/v1/completions` request without overriding the client's. The Hugging Face token is in `~/.dllm/secrets.json` (600).
- **Multimodal:** projector chosen or detected in the model's folder → `llama-server --mmproj … --no-mmproj-offload`.

## Telemetry and per-node settings

- **Agent:** a thread samples every 2 s (3 s on Windows) the GPU it lends, the CPU, the RAM and the bytes going through
  its RPC proxy, and publishes them in `/info` (`telemetry`). For the GPU it uses, in order: `nvidia-smi` (a single
  persistent `-lms` process, with fallback to one-off queries); amdgpu `sysfs` (`gpu_busy_percent`, `mem_info_vram_*`,
  hwmon `temp1_input` in m°C, `power1_average`/`power1_input` and `power1_cap` in µW); on Windows, the PDH API with the
  Task Manager counters ("GPU Engine" → busiest engine usage, "GPU Adapter Memory" → dedicated VRAM), with English names
  so it works in any language; `ioreg` on macOS. If nothing is available, the memory reported by llama.cpp.
- **Orchestrator:** keeps ~5 min of history per node; starts `llama-server` with `--metrics` and reads its `/metrics`
  every 2 s to compute generation and prompt tokens/s and requests in progress. `GET /api/telemetry` serves everything to
  the monitor (incrementally with `?since=`, with the models loaded on each node).
- **Per-node settings** in the orchestrator's `~/.dllm/nodes.json` (by node name): enabled, direct local execution,
  fixed layers, bandwidth and forced mode. The planner assigns the fixed layers first, checks they fit and splits the
  rest as usual.
- **Per-model load settings** → `llama-server` options: `-ctk/-ctv` (cache type), `-fa on|off|auto`, `-b/-ub`, `-np`,
  `--rope-scaling/--rope-freq-base/--rope-freq-scale`, `--jinja`. The split's KV cache size uses the chosen type (bytes per
  element: f16 = 2, q8_0 = 34/32, q4_0 = 18/32…).

## Ports

| Port | Proto | Where | Who connects |
|---|---|---|---|
| 8080 | TCP | Orchestrator | Users / apps (loopback only unless `--expose`) |
| 11434 | TCP | Orchestrator | Ollama-compatible API (`dllm/ollama_api.py`, translates to `/v1`; `--ollama-port 0` disables it) |
| 50999 | UDP | Orchestrator | Node beacons |
| 8765 | TCP | Node | Orchestrator (control API) |
| 50051 | TCP | Node | Orchestrator (model data) |
| ephemeral | TCP | 127.0.0.1 | internal `llama-server`, tunnels, `rpc-server` |

## Installer

- It picks the **most recent `bNNNN` build that publishes all the usual packages** (Windows CPU/Vulkan/CUDA, Linux
  CPU/Vulkan, macOS arm64). It ignores drafts and tags that are not builds, such as `v1.0.0`, which llama.cpp marks as
  "Latest" without binaries (`bNNNN` ones are accepted even if marked pre-release, which is how llama.cpp publishes
  them). So every node finds its package in the same version as the orchestrator.
- It selects the package from [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases) by system,
  architecture and backend: `ubuntu[-vulkan|-rocm-X|-sycl-fp16|-cuda-X]-ARCH` (+ `cudart`), `macos-ARCH`, `win-cpu-ARCH` +
  overlay `win-vulkan|win-rocm-X|win-sycl|win-cuda-X` (+ `cudart`). For CUDA, the highest version the driver supports.
  With the *auto* backend, if one fails it tries the next (CUDA → Vulkan → CPU).
- It verifies the `sha256` published by the GitHub API; extracts rejecting paths outside the destination and escaping
  links.
- It checks the binaries start (`--version`) and gives hints if libraries are missing (Vulkan, libgomp).
- User service: systemd `--user` (Linux), LaunchAgent (macOS), scheduled task at logon (Windows).
- Join code `DLLM1-<base64url(json)>-<crc32>`: `secret`, `orch` (IP), `beacon` (port), `tag` (llama.cpp build), `name`.
  The CRC detects copy errors, it adds no security.
- The installer and the uninstaller are the same web server in two modes (`wizard._WebState.mode`): `setup.html` or
  `uninstall.html`; `/api/uninstall` only exists in uninstaller mode. The installer can restore a backup
  (`Setup.install(restore=…)`).

## Direct local GPU, tools, backups and languages

- **Direct local:** if the plan assigns the whole model to a single device of a node that is this same computer
  (`is_local_host`), `_launch_llama` uses `--device <dev>` without `--rpc`: there is no `rpc-server` or tunnel for that
  slot and `_teardown` does not call `/rpc/stop`. The ledger only reserves that device, so other slots keep using the
  rest of the nodes. The local GPU is identified with `devices.probe_llama_server`; if it does not match, it falls back to
  RPC. `_direct_candidate` says whether it is possible and `_direct_device` applies the preferences (model `local_gpu` >
  node `direct_local` > `--no-direct-local`).
- **Tools:** `gguf.read_model` extracts `chat_template`/`tool_template`; the `tools` setting decides `--jinja`.
  `/v1/models` publishes capabilities; the proxy does not touch `tools`/`tool_calls`.
- **Backups:** `backup.py` (ZIP + sha256 manifest; stdlib encryption PBKDF2 + HMAC) is independent of the orchestrator;
  used by the API, the CLI, the installer and the uninstaller (prior backup).
- **Efficiency:** the panel keeps the telemetry history and asks for `?since=`; `ui.html` is read, compressed and signed
  (ETag) once per process; management JSON responses ≥ 1.4 KB go gzip-compressed if the client accepts it. `/v1` is never
  compressed or buffered.
- **Languages:** the pages are written in Spanish and translated in the browser with `dllm/i18n/en.json` (English by
  default); the terminal and API errors are translated in Python with the same dictionary (`dllm/i18n/__init__.py`).

## Users and permissions

`dllm/users.py` keeps roles → permissions, groups → roles and users → groups in `~/.dllm/users.json` (600), with
PBKDF2-HMAC-SHA256 passwords and API keys stored as SHA-256. The handler resolves the principal (session cookie, local
token from loopback, or a user's API key) and `perm_for(method, path)` gives the permission each route needs; `/v1` from
loopback without credentials is allowed (local apps). Sessions are kept in memory. `DLLM_INSECURE_NO_LOGIN=1` disables
the login only for tests and only with the orchestrator on loopback.

## Limits

- **It does not speed up a stream**: more nodes = larger models, not more tokens/s.
- **No encryption** of traffic (see [SECURITY](SECURITY.md)).
- One device per agent (one `rpc-server`) for its main role: on a computer with several GPUs the one with most memory
  (or the one chosen with `--device`) is lent; a model can use the node's CPU as a separate target. The orchestrator does
  not contribute compute except with the *Both* role.
- llama.cpp is assumed to enumerate remote devices in the order of `--rpc` (one per node).
- Loading sends the weights through the Python tunnel (bottleneck ≈ network). `--cache` on the agent enables
  `rpc-server`'s tensor cache if the build supports it.
- Requests in progress fail during a recovery.
- The multimodal projector runs on the orchestrator's CPU, it is not split. Some Ollama models packaged for its own
  engine cannot be loaded by llama.cpp.
- The Hugging Face and Ollama integration is tested against simulated servers that reproduce their APIs.
- Tested with real llama.cpp on a cluster of 2 Windows computers (AMD Vulkan + NVIDIA) and on a GPU node + CPU-only node
  cluster; the rest, with simulated binaries (see the status in the [README](../README.md)).
