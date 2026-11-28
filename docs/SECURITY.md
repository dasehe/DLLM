# Security

**English** · [Español](es/SEGURIDAD.md)

## Why it matters

llama.cpp's `rpc-server` **has no authentication**: anyone who reaches its port can allocate memory and run operations on
that computer. DLLM never exposes it: it starts it only on `127.0.0.1` and puts a proxy in front that requires proving
knowledge of the cluster secret.

## What DLLM protects

| Surface | Protection |
|---|---|
| `rpc-server` | Listens only on `127.0.0.1`; reachable only through the agent's proxy |
| Node RPC proxy (50051) | **Mutual** HMAC-SHA256 challenge-response handshake before forwarding a single byte |
| Agent API (8765) | Every request signed: HMAC(time, method, path, body sha256); 60 s window; the signature is checked **before** reading the body; 64 MiB limit |
| Ollama-compatible API (11434) | Same rules as the panel/API: loopback by default; exposed only with the API key (`Bearer`); cross-origin browser `POST`s rejected |
| Origin of connections to the node | `allow_ip`: by default the wizard only allows the orchestrator's IP (+ loopback) |
| Discovery (UDP 50999) | Signed beacons; unsigned ones are ignored and logged |
| Panel users | Sign-in always required (also locally); roles/groups/users with per-route permissions; PBKDF2-HMAC-SHA256 passwords; `HttpOnly` + `SameSite=Strict` session cookie (12 h sliding); failed logins → configurable block of the IP and (from the network) the user name, default 5 in 300 s → 300 s; logins recorded in Logs. `admin`/`admin` if no password was set at install (warned at every login) |
| Audit log | Every administrative action (also rejected ones and sign-ins) with user, credential, IP and result in `audit.jsonl` (600, append-only, rotated); secrets replaced by `***`; readable only with the `audit` permission |
| Webhook alerts | Signed with HMAC-SHA256 (`X-DLLM-Signature`, timestamp included); secrets in `webhooks.json` (600), shown once; redirects not followed; `http(s)` URLs without credentials; max. 30 events/min per webhook |
| API keys | One or more per user, inheriting its permissions; optional limit of requests per minute (429 + `Retry-After`); stored as SHA-256, shown once, revocable. The terminal uses a local token (600, loopback only) |
| Orchestrator panel/API (8080) | Only `127.0.0.1` by default; exposing it **requires** an API key (constant-time comparison). Not asked from the computer itself (127.0.0.1). `POST` requests with an `Origin` header from another site are rejected (anti CSRF / DNS rebinding) |
| Join code in the panel | `GET /api/join-code` is only served to clients on the orchestrator itself (the front-ends' API key is not enough) and each request is logged |
| Pairing nodes without a code | Diffie-Hellman (2048-bit MODP group, RFC 3526) + 6-digit verification code compared by eye and accepted **on the node**; the secret travels encrypted and authenticated (HMAC-SHA256, encrypt-then-MAC). Only while the node has no secret; one request at a time, expires in 5 min, max. 10/min |
| Models | Paths are resolved inside the models folder (no `../`) |
| Web wizard / uninstaller | Only `127.0.0.1`, random token in the URL, `Host` check (anti DNS rebinding) and `Origin` check (anti CSRF); closes after 45 min idle |
| Model downloads | Only to the fixed base URLs of Hugging Face and the Ollama registry (the user gives names, not URLs; they are validated). Published sha256 + GGUF header; nothing is used until verified |
| Hugging Face token | `~/.dllm/secrets.json` (600); only sent to huggingface.co: removed from the request if a redirect leads to another host (CDN) |
| Update ordered by the orchestrator | `POST /llama/install` signed with HMAC; only a validated `bNNNN` tag; the agent downloads only from the official ggml-org/llama.cpp releases with sha256. Can be disabled with `--no-auto-update` (node) or `--no-auto-sync` (orchestrator) |
| DLLM update on the nodes | Manual (button); the orchestrator sends its own `dllm.pyz` signed with HMAC (body sha256) plus sha256 and version in headers; the agent checks them, test-runs it (`--version`) before replacing, keeps `dllm.pyz.bak` and rolls back on its own if the new one does not keep running. Can be disabled with `--no-dllm-update` (node) or per node in the panel |
| llama.cpp downloads | sha256 published by GitHub; extraction rejects path traversal, escaping links and special files |
| Backups | Optional encryption with PBKDF2-HMAC-SHA256 (600k iterations) + HMAC; files written with permissions 600; restore checks the integrity tag before decrypting and the sha256 of every file, and only accepts an allowlist of file names |
| Scheduled backups | Always encrypted (same scheme as manual backups), files 600 in a 700 folder; the password lives in `backup_schedule.json` (600) and is never returned by the API; only `dllm-auto-*.dllmbak` files are rotated |
| HTTPS (optional) | `dllm tls enable` (self-signed with openssl, key 600) or own certificate; TLS ≥ 1.2 on the panel/API and the Ollama API; from the network only HTTPS (plain HTTP → 301 to `https://`); session cookie `Secure` over HTTPS; SHA-256 fingerprint shown in the terminal and in *System* |
| Secrets on disk | `~/.dllm/config.json` with permissions 600; the service does not carry the secret in its definition |
| Service | Per user, unprivileged; on Linux `NoNewPrivileges` and `PrivateTmp` |

## What it does NOT protect (accepted risks)

- **Confidentiality**: between the orchestrator and the nodes, prompts, answers and weights travel **unencrypted** over
  the LAN (the RPC tunnel is authenticated, not encrypted). Someone on the same network can read them. Panel and API
  traffic is encrypted only if HTTPS is enabled (`dllm tls enable`).
- **On-demand loading**: it is opt-in (`--on-demand`) because any client with API access could trigger loads and
  evictions; the API key is still required from the network. Only files in the models folder are loaded
  (`resolve_model` rejects paths outside it) and `pinned` models are never evicted.
- **`--allow-ip` on the orchestrator**: filters by source IP before authentication (loopback always allowed).
- **Slots**: the slot number (0-7) is part of the handshake label; each `rpc-server` still listens only on 127.0.0.1 and
  is only reachable through the agent's authenticated proxy.
- **Link probe**: the RPC port handshake accepts a second mode ("probe", different HMAC label) that only allows
  exchanging blocks of up to 8 MiB and 32 requests per connection; it requires the same secret, respects `allow_ip`
  and never reaches `rpc-server`.
- **Replay**: a captured signed request can be reused for 60 s (no *nonce*). The RPC handshake does use random
  challenges.
- **A compromised node** knows the secret: it can impersonate another node or the orchestrator to the other nodes (if
  they have no `allow_ip`). Rotate the secret if you lose control of a computer.
- The **join code** carries the secret in clear (base64). Whoever has it can join the cluster.
- **Default credentials**: if no password was set at install, `admin`/`admin` works (also from the network when the
  orchestrator is exposed) until changed; the panel warns at every login. Change it right after installing.
- Sessions live in memory: restarting the orchestrator signs everyone out.
- `/v1` and the Ollama API accept requests without a key from the orchestrator itself (local apps); any local process can
  use them, as with any service on 127.0.0.1. Management (`/api/*`) always requires a session, a key or the local token.
- Pairing without a code: security depends on **comparing the code** in the panel and on the node. If it is accepted
  without looking, a malicious computer on the network that claims first could take the node (not the cluster secret).
  Announcements from unpaired nodes are not signed (they only reveal name and port).
- An **unencrypted backup** contains the cluster secret and the API key: the panel and the CLI warn and encrypt by
  default.
- `llama-server` and `rpc-server` are third-party software; keep them up to date.

## Pairing without a code

1. The node without a secret announces itself over UDP broadcast (`DLLMPAIR1`, name and port). Its only active routes are
   `/pair/*`; the RPC proxy and the normal API answer 503/close.
2. When claimed, orchestrator and node exchange ephemeral DH public keys (256-bit exponent over the 2048-bit group) and
   derive `K = SHA-256(context ‖ g^xy)`. The verification code is `HMAC(K, public keys)` reduced to 6 digits: a
   man-in-the-middle would have another `K` and, with very high probability, another code.
3. Only when it is accepted on the node (`/pair/local`, reachable only from 127.0.0.1) does the orchestrator send the
   secret with an HMAC-SHA256 counter-mode stream cipher and a MAC over nonce + ciphertext.
4. The node stores the secret, restricts `allow_ip` to the orchestrator's IP and disables pairing.

## Deployment recommendations

1. **Dedicated network**: a VLAN for the cluster, with no access from the Internet or the guest Wi-Fi.
2. **Encryption**: if the network is not trusted, join the computers with **WireGuard** and use the tunnel IPs: in the
   orchestrator's wizard, advanced option *Orchestrator address for the nodes* (`--advertise-host 10.8.0.1`, or
   `dllm join-code --advertise-host 10.8.0.1`), and reinstall the nodes with that code.
3. **Firewall** on the nodes: 8765 and 50051 only from the orchestrator's IP. On Windows the wizard creates those rules
   (per program, port and remote IP); on Linux/macOS it shows the commands (see
   [INSTALLATION](INSTALLATION.md#firewall)). In the *Both* role the agent listens only on `127.0.0.1`.
4. Enable **HTTPS** (`dllm tls enable`, or `--cert/--key` with a certificate from your CA) if the panel or the API are
   used from other computers. Do not expose port 8080 outside the LAN; if you need to, put it behind a reverse proxy
   with TLS and authentication.
5. **Rotate the secret**: the wizard keeps the secret when reinstalling the orchestrator. To generate a new one, delete
   the `"secret"` line from the orchestrator's `~/.dllm/config.json`, run the wizard again and reinstall each node with
   the new code.
6. Treat the API key, the join code and backups like passwords (password manager, not chats or tickets).

## Reporting a security issue

Contact the repository owner directly; do not publish exploitable details in an *issue*.
