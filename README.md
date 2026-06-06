# BookHub

Self-hosted PWA to **search**, **virus-check**, and **download** books
(EPUB/PDF), plus a **PDF → EPUB converter**. Runs on a hardened, network-isolated
VM in the homelab. Single instance, a few users.

> Implementation spec lives in **[BUILD.md](./BUILD.md)** — that is the source of
> truth for building. This README is the operator guide: how to deploy, secure,
> configure, run, and troubleshoot it.

---

## What it does

- Search a book title across **VK + Anna's Archive + Libgen (.li)**, filtered to
  EPUB/PDF, results deduplicated across sources.
- On request, download the file **server-side into quarantine**, verify its
  format, hash it, and check it against **VirusTotal**.
- **Only files with a fresh VirusTotal "clean" verdict are served.** Malicious,
  suspicious, or unverifiable files (too large for VT, quota exhausted, scan
  timed out, stale analysis) are deleted and never served.
- Files are temporary: deleted after download and swept by a TTL cleaner.
- Convert an uploaded PDF to EPUB (Calibre + OCR), run in a **gVisor sandbox with
  no network**.
- Username/password auth, admin panel (users + audit events + provider health +
  VT quota).

### Honest limits (read these)

- **Converter quality:** Calibre reflows text; it does **not** rebuild tables or
  multi-column layouts well. Novels/single-column PDFs convert cleanly; complex
  layouts are best-effort.
- **Size cap:** with strict scanning, books larger than **32 MB** cannot be
  VirusTotal-verified and therefore will **not** download. This is intentional;
  tunable via `DOWNLOAD_MAX_MB` (raising it means the 32 MB+ band stays blocked
  under strict mode).
- **Source fragility:** Libgen mirrors die/rotate and sit behind Cloudflare; VK
  `docs.search` can return access-denied for some tokens. The app fails soft and
  leans on whichever sources are alive.

### Legal

This tool downloads from sources that distribute copyrighted material. Running it
and any resulting liability are the operator's responsibility. Keep it private
(Cloudflare Access / Tailscale), not openly public. Per-user download history is
TTL-pruned; do not treat it as a long-term record.

---

## Architecture at a glance

```
                        Cloudflare edge (TLS + Access identity gate)
                                     |  (outbound 443 only)
                              [ cloudflared ]            <-- Option A: sidecar on this VM
                                     |  proxy_net (internal)
   isolated VLAN  ->  [ Proxmox VM ]  [ bookhub app (FastAPI) ]
   OPNsense egress:                        |          |
   - allow internet (VK/AA/Libgen/VT)      |          | launches per-job (via socket-proxy)
   - DENY all RFC1918 + management         |          v
                                           |   [ gVisor worker: Calibre+OCR ]
                                           |     --network=none, --read-only,
                                           |     only the job's tmp dir mounted
                                           v
                                   /data (app.db, quarantine/, ready/, jobs/)
```

- **Host:** dedicated Proxmox **VM** (not LXC — gVisor needs a real kernel/Docker)
  on its **own isolated VLAN**.
- **Egress firewall (OPNsense):** allow internet to the sources + VirusTotal,
  **deny all RFC1918 and management**. This blocks lateral movement to
  Immich/CasaOS/Proxmox/OPNsense even if the app is compromised.
- **Converter sandbox (gVisor):** untrusted PDFs are parsed in a disposable
  `runsc` container with no network and no access to `app.db`/`.env`.
- gVisor and the VLAN are **separate layers**: gVisor stops a parser escaping to
  the VM kernel; the VLAN/firewall stops a network-level compromise reaching
  other services. You need both.

---

## Prerequisites

1. **Proxmox VM** (full HVM, e.g. 4 vCPU / 4 GB RAM / 20 GB disk), Debian/Ubuntu,
   running only this stack. Not an LXC. Not the host that runs your other dockers.
2. **Docker + Docker Compose** on the VM.
3. **gVisor (`runsc`) runtime** installed and registered with Docker (below).
4. **OPNsense:** a dedicated VLAN for this VM + the egress rules (below).
5. **Cloudflare** account with a tunnel + (recommended) a Zero Trust **Access**
   policy.
6. API access: a **VirusTotal** free API key; optionally a **VK** user token and
   an **Anna's Archive** donor key.

### Install gVisor (runsc) on the VM

```bash
# Cloudflare-hosted gVisor apt repo (see gvisor.dev/docs for the current steps)
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install            # registers the runtime with Docker
sudo systemctl restart docker
```

Register it in `/etc/docker/daemon.json`:

```json
{ "runtimes": { "runsc": { "path": "/usr/bin/runsc" } } }
```

Verify: `docker run --rm --runtime=runsc hello-world`.

### OPNsense VLAN + egress rules

Create an isolated VLAN (e.g. `VLAN 50 / 10.50.0.0/24`), assign the VM's vNIC to
it, then on that VLAN interface add **floating/interface rules** (top to bottom):

| Action | Source        | Destination               | Port      | Note |
|--------|---------------|---------------------------|-----------|------|
| Pass   | VLAN50 net    | (this firewall) DNS        | 53        | resolver only |
| Block  | VLAN50 net    | `10.0.0.0/8`               | any       | deny RFC1918 |
| Block  | VLAN50 net    | `172.16.0.0/12`            | any       | deny RFC1918 |
| Block  | VLAN50 net    | `192.168.0.0/16`           | any       | deny RFC1918 |
| Block  | VLAN50 net    | `169.254.0.0/16`           | any       | link-local/metadata |
| Pass   | VLAN50 net    | any (internet)             | 80,443    | sources + VT |

**Option B only** (reuse the services-VLAN cloudflared): add one inbound allow on
the **services** VLAN interface — `source = services cloudflared IP`,
`dest = BookHub VM IP`, `port 8000`. OPNsense is stateful, so the reply is
allowed automatically; the VM still cannot initiate into RFC1918.

**Option A** (cloudflared sidecar) needs **no** inbound rule — only the
`Pass ... 443` egress already in the table.

---

## Configuration

Copy and edit the env file:

```bash
cp .env.example .env
```

Key settings (full list in `.env.example` / BUILD.md §2):

| Var | What |
|-----|------|
| `VT_API_KEY` | VirusTotal API key (required for downloads to be served) |
| `VK_TOKEN` | VK user access token (optional; empty = VK disabled) |
| `AA_API_KEY` | Anna's Archive donor key (optional; empty = HTML fallback) |
| `LIBGEN_MIRRORS` | comma list of `.li`-family mirrors, reorderable |
| `DOWNLOAD_MAX_MB` | download size cap (default 32, matches VT upload cap) |
| `CONVERT_MAX_MB` | converter upload cap (default 200; user's own file) |
| `FILE_TTL_MINUTES` | how long served/quarantined files live (default 60) |
| `ADMIN_PASSWORD` | bootstrap admin password; if empty, a random one is logged once |
| `COOKIE_SECURE` | set `true` behind the Cloudflare Tunnel (TLS at edge) |
| `CLOUDFLARED_TOKEN` | Option A sidecar tunnel token |

### Get a VirusTotal API key (free)

1. Create an account at virustotal.com → profile → **API key**.
2. Paste into `VT_API_KEY`. Free tier: 4 req/min, 500/day, 32 MB upload.

### Get a VK user token (optional)

VK `docs.search` needs a **user** token with the `docs` scope (community tokens
cannot call it).

1. Create a VK **Standalone** app (vk.com/dev → My apps).
2. Build an OAuth **implicit-flow** URL with `scope=docs,offline` and your app id,
   open it, authorize, and copy the `access_token` from the redirect URL fragment.
3. Paste into `VK_TOKEN` (or set it later in the admin panel — it overrides env).

> If VK returns error 5 or 15, the provider self-disables and the admin panel
> shows "VK disabled — re-paste token". This is common; VK is best-effort.

### Get an Anna's Archive donor key (optional)

A donor membership exposes a JSON API key that makes the AA provider far more
robust (no Cloudflare/HTML scraping). Without it, AA falls back to HTML search.

### Set up the Cloudflare Tunnel (Option A, recommended)

1. Cloudflare Zero Trust dashboard → **Networks → Tunnels → Create tunnel**.
2. Copy the tunnel **token** into `CLOUDFLARED_TOKEN` in `.env`.
3. Add a **public hostname** (e.g. `books.example.com`) → service
   `http://bookhub:8000`.
4. **Zero Trust → Access → Applications**: add a self-hosted app for that
   hostname with an identity policy (your email / SSO). This gates the app before
   any request reaches BookHub's own login.
5. Leave the `cloudflared` service in `docker-compose.yml` enabled and remove the
   app's published port (see BUILD.md §11.3).

---

## Build and run

```bash
docker compose build           # builds app + converter images
docker compose up -d
docker compose logs -f bookhub # watch startup; first-run admin password prints here if ADMIN_PASSWORD unset
```

- First launch initializes `data/app.db` and the `quarantine/`, `ready/`, `jobs/`
  directories.
- Open the app (via the Cloudflare hostname, or `http://<vm-ip>:8000` on the LAN
  if you kept a published port for testing). Log in as `admin`, change the
  password, then create users in the admin panel.

Health check: `GET /api/health` (no auth) — used by the compose healthcheck.

---

## Operating it

- **Admin → Events:** blocked files (with VT detection counts, persisted even
  after the file is deleted), which mirror served each search, provider health,
  and VirusTotal quota remaining.
- **VT quota:** the app stops calling VT before 500/day; affected downloads
  become `unverified` (not served) with a "retry later". Watch the Events view if
  downloads start failing late in the day.
- **VK token expiry:** re-paste in the admin panel; no container restart needed.
- **Dead mirror:** reorder/replace `LIBGEN_MIRRORS` in `.env` and
  `docker compose up -d` (no rebuild needed).

---

## Backup and restore

Only `data/app.db` and `.env` matter (`quarantine/`, `ready/`, `jobs/` are
ephemeral).

```bash
# consistent DB snapshot (NOT a raw cp of a live WAL db)
docker compose exec bookhub sqlite3 /data/app.db ".backup '/data/app.db.bak'"
# then copy app.db.bak and .env off-box (pull from the host/backup VM)
```

Restore: drop `app.db` into `data/`, restore `.env`, `docker compose up -d`. The
ephemeral dirs rebuild empty.

---

## Upgrading

```bash
git pull
docker compose build --pull    # Calibre image is large; rebuilds are slow
docker compose up -d
```

Calibre version drift can change `ebook-convert` flags; the conversion command is
kept minimal and tolerant. Pin `python:3.12-slim` by digest and pin pip versions
(in `requirements.txt`).

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| Search returns nothing from Libgen | Mirror dead or under Cloudflare challenge → reorder `LIBGEN_MIRRORS`; check Events for "Cloudflare challenge" log lines. |
| VK never returns results | Token denied (error 5/15) → re-mint a user token with `docs` scope; check admin provider health. |
| Downloads stuck at "Checking (VirusTotal)" then "unverified" | VT daily quota hit, scan timed out, or file > 32 MB → see the `reason`; retry later for quota/timeout. |
| Everything `unverified: too_large` | File exceeds `DOWNLOAD_MAX_MB` / VT's 32 MB upload cap — cannot be scanned under strict mode. |
| Conversion fails immediately | Check the worker launched under `runsc`; verify `docker run --runtime=runsc hello-world` works on the VM. |
| Conversion produces garbled text | Scanned PDF / wrong OCR language → pick the right `OCR_LANGS` (or per-job language); complex layouts/tables are best-effort. |
| App logs everyone out on restart | Should not happen — sessions are opaque DB tokens, not signed cookies. If it does, check `data/app.db` is on the persistent volume. |
| Can't reach the app via tunnel | Cloudflare Access policy blocking you, or ingress points at the wrong origin; check the tunnel's public-hostname → `http://bookhub:8000`. |

---

## Security posture (summary)

- Dedicated VM on an isolated VLAN; OPNsense denies RFC1918 + management egress.
- App container: non-root, read-only rootfs, `cap_drop ALL`, no-new-privileges,
  mem/pids/cpu limits, loopback/internal bind only.
- Converter: gVisor `runsc`, `--network=none`, only the job dir mounted, no
  secrets in its env. Launched via a scoped docker-socket-proxy, never the raw
  socket.
- Downloads: format-verified (zip-bomb/polyglot guarded, no extraction) → only
  served on a **fresh** VirusTotal "clean".
- Auth: argon2 hashes, opaque DB-validated session tokens, login rate limiting.
- Access: Cloudflare Tunnel (outbound-only) + Cloudflare Access identity gate.

See **BUILD.md §15** for the full security checklist the build must satisfy.
