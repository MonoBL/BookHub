# BUILD.md — BookHub (self-hosted book search + download + convert PWA)

> **Audience:** this document is the full build spec. It is written for a Claude
> Sonnet session to implement end-to-end. Follow it milestone by milestone, in
> order. Do not invent extra features. Ask Nuno only when something here is
> genuinely ambiguous.
>
> **This is v2.** It was rewritten after a 6-lens security/architecture review
> (68 findings: 5 critical, 21 high). Decisions made by Nuno are recorded in
> §0. Where this spec contradicts an "obvious" simpler approach, the simpler
> approach was reviewed and rejected for a stated reason — do not silently
> revert it.

---

## 0. Decisions already made (do not relitigate)

| # | Decision | Choice |
|---|----------|--------|
| Host | Where BookHub runs | A **dedicated Proxmox VM** on the homelab (decided), on its **own isolated VLAN**. **Not** a shared LXC, **not** the services VLAN (Immich/CasaOS = crown jewels), **not** nested in another docker host. See §3. |
| Converter sandbox | How untrusted PDFs are parsed | **gVisor (runsc) ephemeral worker container, no network.** This is the single most important security control. See §8. |
| VirusTotal policy | What happens to unscannable files | **Block all unscanned.** Never serve a file without a fresh "clean" verdict. No `ALLOW_UNSCANNED_LARGE`. See §7.4. |
| Sources | Which providers ship in v1 | **VK + Anna's Archive + Libgen (.li family).** All three, deduped. VK is enabled (Nuno reports it works for him). See §6. |

**Critical interaction:** gVisor + the host choice. gVisor's `runsc` runtime runs
cleanly inside a **full VM** but does **not** run reliably nested inside an
unprivileged Proxmox **LXC** (it needs kernel features / a real Docker daemon
that LXC restricts). This is why the host is a **dedicated VM**, not an LXC.

**gVisor does NOT replace network isolation — they are orthogonal controls:**
- gVisor sandboxes the **converter's syscalls** so a parser RCE can't escape to
  the VM's kernel. It does nothing about the network.
- The **app** container still needs outbound network (VK, Anna's Archive, Libgen,
  VirusTotal). If the app itself is compromised (dependency vuln, SSRF, parsing a
  hostile Libgen page), the attacker is live on **whatever VLAN the VM sits on**.
- Therefore the VLAN + OPNsense egress firewall (§3.2) is required *in addition
  to* gVisor. One guards the converter-escape path; the other guards the
  app-compromise path.

---

## 1. What we are building

A PWA hosted on Nuno's homelab that:

1. Lets a logged-in user **search a book by name** across pluggable sources
   (v1: VK, Anna's Archive, Libgen .li), filtered to **EPUB/PDF**, results
   deduplicated across sources.
2. On user request, **downloads the file server-side into a quarantine dir**,
   verifies its format (magic bytes / real-zip check), hashes it, and **checks
   it against VirusTotal**.
3. **Only files with a fresh VirusTotal "clean" verdict are served.** Anything
   malicious, suspicious, or that VirusTotal cannot verify (too large, quota
   exhausted, scan timed out, stale/thin analysis) is **not served**. See §7.4.
4. Files are **temporary**: deleted after the user downloads them, and swept by
   a TTL cleaner regardless.
5. Has a second page to **upload a PDF and convert it to EPUB** (Calibre +
   OCR), run inside a **gVisor sandbox with no network**.
6. Has **username/password auth** with an **admin panel** to create users and
   an **events/audit view** (blocked files, provider health, VT quota).

Personal homelab tool, single instance, low traffic (a few users).

### 1.1 Honest scope of the PDF→EPUB converter (read this — do not over-promise)

Calibre's PDF input is **text-extraction-and-reflow, not a layout engine.** It
does **not** reconstruct tables (cells dump as scrambled text runs), it
interleaves multi-column text, and it floats/drops figures and footnotes. The
original ask was "ready for everything, image, table, etc." — that is **not
achievable** with Calibre (or any free CPU-only tool) for complex PDFs.

What the converter will honestly do:
- **Text-first PDFs** (novels, single-column reports): convert well.
- **Scanned PDFs**: OCR first (ocrmypdf), then convert. Quality = OCR quality.
- **Tables, multi-column, footnoted academic PDFs**: **best-effort, degrades.**
  The UI must say this plainly (§9, convert page).

A heavier ML tool (Marker) does tables far better but is too slow/heavy for a
CPU homelab box — **deliberately deferred** to §13, not silently ignored.

---

## 2. Stack and constraints (decided, do not change)

| Area        | Decision |
|-------------|----------|
| Backend     | Python 3.12, FastAPI, uvicorn |
| Frontend    | Static PWA: vanilla JS + HTML + CSS served by FastAPI. No React/Vue/build step. |
| DB          | SQLite via **aiosqlite**, WAL mode, single-writer discipline (see §13). No ORM, no connection pool. |
| Conversion  | Calibre `ebook-convert` + `ocrmypdf`, run in a **gVisor (runsc) ephemeral worker container with `--network=none`** (see §8) |
| Deploy      | Docker Compose on a dedicated isolated host (NUC or VM), hardened (see §3, §11) |
| Auth        | Server-side **opaque session tokens** (no SECRET_KEY/signed cookies — see §9). Passwords hashed with `argon2-cffi`. |
| HTTP client | `httpx` (async) |
| HTML parsing| `selectolax` (preferred) or `beautifulsoup4` for Libgen/AA pages |
| Code style  | Per Nuno's rules: simple, readable, stdlib-preferred, no over-engineering, comments only on non-obvious logic, English only, no em dashes. |

Secrets/config via `.env` loaded with `pydantic-settings`:

```
# --- core ---
DATA_DIR=/data
COOKIE_SECURE=false            # set true behind Cloudflare Tunnel (TLS at edge)
ADMIN_PASSWORD=                # if set, bootstrap admin with it; else random+logged once
CLOUDFLARED_TOKEN=             # Option A: token for the cloudflared sidecar tunnel (§3.5)

# --- limits ---
DOWNLOAD_MAX_MB=200            # keep <= 650: VT's large-file upload limit. Above it -> unverified. See §7.4.
CONVERT_MAX_MB=200             # converter upload cap (user's own file, gVisor-sandboxed)
FILE_TTL_MINUTES=60            # quarantine/ready file lifetime
DOWNLOAD_CONCURRENCY=3
SCAN_CONCURRENCY=3
CONVERT_CONCURRENCY=1

# --- VirusTotal ---
VT_API_KEY=
VT_CLEAN_MAX_AGE_DAYS=180      # re-scan "clean" hashes older than this (freshness floor)
VT_MIN_ENGINES=40             # require >= this many engines reported before trusting clean
VT_DAILY_CAP=480               # stop calling VT before the 500/day free-tier limit

# --- providers ---
VK_TOKEN=                      # user access token w/ docs scope; empty -> VK disabled
AA_API_KEY=                    # Anna's Archive donor key (optional); empty -> AA uses HTML search
LIBGEN_MIRRORS=libgen.la,libgen.li,libgen.vg,libgen.gl   # .li family, comma list, reorderable
PROVIDER_SEARCH_TIMEOUT_S=15
PROVIDER_RESOLVE_TIMEOUT_S=30

# --- converter sandbox ---
RUNSC_RUNTIME=runsc            # docker runtime name for the worker
CONVERTER_IMAGE=bookhub-converter:latest
CONVERT_TIMEOUT_S=600
OCR_TIMEOUT_S=1200
OCR_LANGS=eng+por+fra+spa
```

`.env.example` committed; `.env` git-ignored. No secret is ever sent to the
frontend or written to logs.

---

## 3. Deployment topology and isolation (the load-bearing section)

BookHub's core job is **running untrusted file parsers** (Calibre, ghostscript,
poppler, ocrmypdf, tesseract) over **attacker-controlled PDFs** on the converter
page. A parser RCE is the **expected** compromise to design against, not a tail
risk. Every control below assumes the converter *will* eventually be popped and
asks: what can the attacker reach next?

### 3.1 Host (decided: dedicated VM on the homelab)

- A dedicated Proxmox **VM** (full HVM, separate kernel), running only this
  Docker stack, **not** the VM/host that runs the other dockers, **not** an LXC.

**VLAN placement (decided priority order):**
1. **Own isolated VLAN (do this).** A "DMZ/untrusted" VLAN holding only this VM.
   Cheap on OPNsense: one VLAN tag + interface + one ruleset (§3.2). This is the
   correct choice and matches the rest of the spec.
2. **Gaming VLAN** — acceptable fallback *only* if it holds no other valuable
   hosts and is firewalled from the LAN. Lesser evil.
3. **Services VLAN (Immich/CasaOS) — NO.** A box whose job is ingesting
   attacker-controlled files must not share an L2 segment with personal photos
   and other services. Within a VLAN, hosts reach each other freely (same
   broadcast domain) unless you deploy private-VLAN/host-isolation, which
   homelabs typically don't. A compromise here would have direct east-west
   access to Immich. Rejected.

Note: this VLAN decision stands **regardless of gVisor** — see the
gVisor-vs-network note in §0. gVisor protects the VM kernel from a converter
escape; it does not change which segment a network-level app compromise can
reach.

### 3.2 Network egress (the choke point)

Whatever the host, its NIC sits on an **isolated VLAN** and OPNsense enforces:

- **ALLOW** outbound to the internet (VK API, Anna's Archive, VirusTotal, Libgen
  mirrors + their download CDN).
- **DENY** all RFC1918 (`10/8`, `172.16/12`, `192.168/16`), link-local
  `169.254/16` (cloud-metadata), and the management VLAN (Proxmox `:8006`,
  OPNsense GUI, Home Assistant).
- Allow DNS only to the chosen resolver.

This is the rule that stops a compromised box from pivoting to the firewall or
hypervisor. Write it on OPNsense (stateful, logged, auditable) rather than
juggling host `iptables`.

**Inbound exception for remote access:** with **Option A** (cloudflared sidecar,
§3.5) there is **no inbound exception** — the deny-RFC1918 egress rule is the
whole story. With **Option B** (reuse the services-VLAN cloudflared), add exactly
one inbound allow on OPNsense (services cloudflared IP → BookHub VM:8000); the
stateful firewall handles the reply, and BookHub still cannot *initiate* into
RFC1918.

If for some reason egress rules must live on the Docker host instead, use the
`DOCKER-USER` chain (Docker's NAT bypasses normal `INPUT` rules):

```
iptables -I DOCKER-USER -i br-bookhub -d 10.0.0.0/8     -j DROP
iptables -I DOCKER-USER -i br-bookhub -d 172.16.0.0/12  -j DROP
iptables -I DOCKER-USER -i br-bookhub -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -i br-bookhub -d 169.254.0.0/16 -j DROP
# allow established, allow DNS, allow internet, drop the rest of RFC1918
```

### 3.3 Container hardening (defense in depth inside the host)

The app container is not the security boundary (the VLAN/host is), but harden it
anyway. See the exact compose block in §11. Summary: non-root uid, `read_only`
rootfs, `cap_drop: [ALL]`, `no-new-privileges`, keep default seccomp, `pids_limit`,
`mem_limit`, `cpus`, `tmpfs:/tmp` with `noexec,nosuid,nodev`, bind to
`127.0.0.1` only (reverse proxy fronts it), dedicated user-defined bridge.

### 3.4 Hard prohibitions (never, in any compose/run)

- **Never** mount `/var/run/docker.sock` into the app container directly.
- **Never** `privileged: true`, `network_mode: host`, `pid: host`, `ipc: host`.
- **Never** pass `seccomp=unconfined`.
- The converter worker is launched via a **scoped docker-socket-proxy**
  (§8.3), never the raw socket.

### 3.5 Access (how Nuno reaches the PWA) — via Cloudflare Tunnel

Remote access is through **Cloudflare Tunnel** (cloudflared). The tunnel makes
only **outbound 443** to the Cloudflare edge and proxies inbound requests to the
origin — so it never requires an open inbound port on the homelab. Two ways to
wire it; **Option A keeps the VLAN isolation fully intact and is the chosen
default.**

**Option A (recommended): cloudflared as a sidecar on the BookHub VM.**
- Run a second `cloudflared` container inside BookHub's own compose (its own
  tunnel + public hostname). It joins only the `proxy_net` docker network with
  the app and reaches it at `http://bookhub:8000` (internal).
- BookHub exposes **no port** on any VLAN (stays loopback/internal only).
- cloudflared's only network need is **outbound 443 to Cloudflare**, already
  permitted by the §3.2 "allow internet" rule. **No cross-VLAN rule, no hole in
  the deny-RFC1918 posture.**

**Option B: reuse the existing cloudflared in the services VLAN.**
- That cloudflared (services VLAN) must reach BookHub, so add **one narrow
  stateful allow** on OPNsense: source = services cloudflared host IP, dest =
  BookHub VM IP, port `8000` (or the proxy port). BookHub binds to its VLAN IP
  (not just loopback) for this.
- Because OPNsense is **stateful**, BookHub's reply traffic on that established
  session is auto-allowed even though §3.2 denies BookHub-initiated RFC1918.
  BookHub still **cannot initiate** new connections into services/LAN, so
  lateral movement stays blocked. The hole is inbound-only, single-source,
  single-port.
- Weaker than A (a port is reachable from the services VLAN); choose only if
  running a second cloudflared is undesirable.

**Regardless of option:**
- Put **Cloudflare Access** (Zero Trust) in front of the tunnel hostname — an
  identity gate (email/SSO) *before* requests ever reach BookHub's own login.
  Free, and a strong second layer. Strongly recommended since the tunnel makes
  the app internet-reachable.
- TLS is terminated at the Cloudflare edge → set `COOKIE_SECURE=true`. Run
  uvicorn with `--proxy-headers`; trust cloudflared's forwarded IP and derive the
  login rate-limit key from `CF-Connecting-IP` / validated `X-Forwarded-For`.
- Do **not** route BookHub through the same proxy/tunnel ingress that fronts
  Proxmox/OPNsense/HA admin UIs; keep it a distinct hostname/ingress.
- Tailscale/WireGuard remains a valid private alternative if Nuno prefers no
  public hostname at all.

### 3.6 Backup

- Only `${DATA_DIR}/app.db` and `.env` need backup. `quarantine/` and `ready/`
  are ephemeral.
- Snapshot the DB consistently: `sqlite3 app.db ".backup '/backup/app.db'"` (or
  `VACUUM INTO`), never a raw `cp` of a live WAL DB.
- Backups must be **pulled** from outside (host cron or a backup VM); BookHub
  has no write path to the backup store (consistent with the egress-deny rule).

---

## 4. Repository layout

```
.
├── BUILD.md
├── docker-compose.yml
├── Dockerfile                # main app image
├── Dockerfile.converter      # converter worker image (Calibre + ocrmypdf + tesseract)
├── .env.example
├── .gitignore
├── requirements.txt          # pinned versions
├── app/
│   ├── main.py               # FastAPI app, routers, startup tasks (cleaner, db init)
│   ├── config.py             # pydantic-settings
│   ├── db.py                 # aiosqlite, WAL, single-writer lock, schema migration
│   ├── auth.py               # opaque-token sessions, argon2, admin guard, rate limit
│   ├── models.py             # pydantic models (SearchResult, Job, User, Event)
│   ├── events.py             # structured logging + events-table audit writer
│   ├── providers/
│   │   ├── __init__.py        # PROVIDERS registry
│   │   ├── base.py            # Provider protocol + SearchResult + ProviderStatus
│   │   ├── vk.py
│   │   ├── annas.py
│   │   └── libgen.py
│   ├── services/
│   │   ├── search.py          # fan-out, per-provider status, dedup, result cache
│   │   ├── downloader.py      # fetch -> quarantine (stall guard, size cap)
│   │   ├── scanner.py         # format verify + VirusTotal (freshness floor, token bucket, quota)
│   │   ├── converter.py       # orchestrates gVisor worker container per job
│   │   ├── jobs.py            # in-memory job registry (asyncio.Lock, in-flight set, pruning)
│   │   └── cleaner.py         # TTL sweep + session/job/history pruning
│   └── routers/
│       ├── pages.py           # serves static pages, redirects unauthenticated to /login
│       ├── api_search.py
│       ├── api_jobs.py
│       ├── api_convert.py
│       ├── api_files.py        # serve + delete-after-send
│       ├── api_auth.py
│       └── api_admin.py        # users + events + provider health + VT quota
├── converter/
│   └── run_convert.py         # entrypoint INSIDE the worker: reads /work/in.pdf -> /work/out.epub
├── static/
│   ├── index.html  convert.html  admin.html  login.html  change-password.html
│   ├── app.js  search.js  convert.js  admin.js
│   ├── style.css
│   ├── manifest.webmanifest
│   ├── sw.js                  # network-first for nav/JS, never touches /api
│   └── icons/                 # 192, 512, 512-maskable, apple-touch 180
├── data/                      # volume (gitignored): app.db, quarantine/, ready/, jobs/
└── tests/
    ├── test_scanner.py  test_providers.py  test_auth.py  test_converter.py
    └── fixtures/              # recorded HTML, tiny real epub/pdf, exe-renamed-pdf
```

---

## 5. Database schema (create on startup if missing)

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,   -- column, NOT a session flag
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,            -- secrets.token_urlsafe(32); opaque, DB-validated
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);

-- VirusTotal verdict cache with freshness metadata
CREATE TABLE IF NOT EXISTS vt_cache (
    sha256 TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,             -- 'clean' | 'malicious' | 'suspicious'
    malicious_count INTEGER,
    suspicious_count INTEGER,
    engines_total INTEGER,             -- for the min-engines floor
    last_analysis_date TEXT,           -- for the freshness floor
    checked_at TEXT NOT NULL
);

-- download history (per-user "recent")
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT, author TEXT, source TEXT, ext TEXT, sha256 TEXT,
    verdict TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, created_at);

-- audit/events: blocked files, provider health, VT quota, serves. The product value.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,                -- 'search'|'download'|'verdict'|'block'|'serve'|'sweep'|'vt_quota'|'provider'
    user_id INTEGER,
    title TEXT, source TEXT, sha256 TEXT,
    detail TEXT                         -- JSON blob: counts, reason, mirror, etc.
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
```

**First-run admin bootstrap:** if `users` is empty:
- if `ADMIN_PASSWORD` is set and non-empty (min length 12): create `admin` with it.
- else: generate a random password, log it to container stdout **once** (documented
  exception to no-secrets-in-logs), set `must_change_password=1`.
On a `must_change_password=1` user, every page except the change-password form
redirects there until they change it.

**Pruning** (in the cleaner loop, §7.5): delete expired `sessions`; keep latest
500 `history` rows per user; cap `events` (e.g. 10k rows or 90 days); drop
terminal jobs from the in-memory registry older than TTL.

---

## 6. Search providers

### 6.1 Provider protocol (`providers/base.py`)

```python
class SearchResult(BaseModel):
    id: str                # provider-scoped opaque id
    title: str
    author: str | None
    ext: str               # 'epub' | 'pdf'
    size_bytes: int | None
    source: str            # 'vk' | 'annas' | 'libgen'
    extra: dict = {}       # provider internals (md5, mirror, vk url, AA id...)

class ProviderStatus(BaseModel):
    name: str
    status: str            # 'ok' | 'error' | 'timeout' | 'disabled'
    count: int = 0
    note: str | None = None

class Provider(Protocol):
    name: str
    enabled: bool
    async def search(self, query: str, ext_filter: list[str]) -> list[SearchResult]: ...
    # resolve returns everything needed to fetch, not just a URL:
    async def resolve(self, result: SearchResult) -> DownloadPlan: ...

class DownloadPlan(BaseModel):
    url: str
    headers: dict = {}     # e.g. Referer
    cookies: dict = {}     # session cookies from the resolve handshake
```

### 6.2 Search service (`services/search.py`)

- Fan out to all enabled providers concurrently (`asyncio.gather`,
  `return_exceptions=True`), per-provider timeout `PROVIDER_SEARCH_TIMEOUT_S`.
- A provider failing/timing out must NOT fail the whole search. Return:
  `{results: [...], providers: [ProviderStatus, ...]}`. The UI shows a banner
  per errored provider ("Libgen unavailable, results may be incomplete").
- **Cross-source dedup:** group by `normalize(title) + normalize(author) + ext`
  (lowercase, strip punctuation/whitespace). Collapse duplicates into one row
  that carries multiple download options (one per source). Within Libgen, dedup
  by md5 (authoritative).
- **Result cache:** in-memory dict keyed by `(provider, normalized_query,
  ext_filter)`, TTL 5–10 min, to avoid hammering fragile mirrors. No Redis.

### 6.3 VK provider (`providers/vk.py`) — enabled

- VK API `docs.search` (requires a **user** access token with `docs` scope;
  community tokens cannot call it). API `v=5.199`.
  `GET https://api.vk.com/method/docs.search?q=<query>&count=50&access_token=…&v=5.199`
- Items: `title`, `ext`, `size`, `url` (direct, short-lived). Filter
  `ext in (epub, pdf)`.
- `resolve`: if the stored `url` is fresh use it; if it 403s/expired, re-run a
  narrow `docs.search` by title to get a new URL.
- `VK_TOKEN` empty → `enabled = False`, status `disabled`.
- On VK error 5 or 15 (auth/access-denied): mark provider `disabled` for the
  process lifetime, log it, and surface it on the admin provider-health view
  ("VK: disabled — token expired/denied, re-paste VK_TOKEN"). Do not retry-loop.
- **Admin can set `VK_TOKEN` via the admin panel** (stored in DB, overrides
  env) so re-auth does not require editing `.env` + recreating the container.
- README: how to mint a VK standalone-app user token (`scope=docs,offline`).

### 6.4 Anna's Archive provider (`providers/annas.py`)

- The most **stable** source (it survives Libgen mirror churn). Primary
  long-term source.
- If `AA_API_KEY` (donor key) is set: use the JSON API — removes most Cloudflare/
  parser fragility. (Implement search + a resolve that returns a download URL.)
- If no key: HTML search on the current AA domain, parse result cards, resolve to
  a download link. Apply the same Cloudflare-challenge detection as §6.5.
- Filter to epub/pdf.

### 6.5 Libgen provider (`providers/libgen.py`) — .li family only

> The original spec's mirrors (`libgen.is/.rs/.st`, `library.lol`) and the
> `search.php` path were **verified dead** on 2026-06-06. Build to current
> reality.

- **Mirrors:** from `LIBGEN_MIRRORS` env (default `libgen.la, libgen.li,
  libgen.vg, libgen.gl`). Health-probe in order (`GET /`, <500, 5s); first live
  mirror wins, cached 10 min — but **demote immediately** if a real search
  returns a challenge/error page (don't wait out the cache).
- **Search:** use **`index.php?req=<query>`** (NOT `search.php` — it 404s on every
  live mirror). Parse the results table by **`id="tablelibgen"`** and select
  columns by **header name** (…Language, Pages, Mirrors), never by fixed index.
  Filter rows to `ext in (epub, pdf)`. Grab the md5.
- **Cloudflare detection:** if a response is 403/503 or the body contains
  `Just a moment`, `challenge-platform`, or `cf_chl`, treat the mirror as DOWN
  and rotate — do **not** return an empty table as "no results". Log
  `mirror X under Cloudflare challenge`.
- **Download resolution (`resolve`)** owns the full handshake:
  1. `GET /ads.php?md5=<md5>` (with browser `User-Agent`), keep cookies.
  2. Parse the `get.php?md5=…&key=…` link (the **key is mandatory** —
     `get.php` without it 307s to a non-serving path).
  3. Return a `DownloadPlan{url, headers={Referer: ads.php URL}, cookies}`.
     The real file is served from a separate CDN host (e.g. `booksdl.*`); the
     `Referer` + cookies are required or the CDN refuses.
- `resolve` timeout = `PROVIDER_RESOLVE_TIMEOUT_S` (30s; multi-hop through
  Cloudflare + CDN), separate from the 15s search timeout.
- Set a real browser `User-Agent` on every request.
- Do **not** add FlareSolverr/headless-browser for v1; fail-soft + rotate.

### 6.6 Adding sources later

Implement the protocol, register in `providers/__init__.py` `PROVIDERS`. Nothing
else changes.

---

## 7. Download + scan pipeline (the core flow)

### 7.1 Job model

`POST /api/download` `{result}` → creates a job, returns `{job_id}` (UUID4).
Frontend polls `GET /api/jobs/{id}`.

Statuses: `queued → downloading → verifying → scanning → clean | blocked | unverified | error`

- `clean` → served.
- `blocked` → malicious/suspicious (permanent, file deleted).
- `unverified` → VT couldn't verify (too large / quota / timeout / stale). **Not
  served.** Distinct from `blocked` so the UI can say "retry later" for transient
  causes. Carries a `reason` field.
- Jobs live in an in-memory dict (`services/jobs.py`), guarded by one
  `asyncio.Lock`. A restart loses in-flight jobs (the UI handles a 404 from
  `/api/jobs/{id}` as "job lost, server restarted, retry").
- `DOWNLOAD_CONCURRENCY` semaphore (default 3); excess wait in `queued`.

### 7.2 Download (`services/downloader.py`)

1. `provider.resolve(result)` → `DownloadPlan`.
2. Stream into `${DATA_DIR}/quarantine/<job_id>.<ext>` with httpx using the
   plan's headers + cookies, `follow_redirects=True`:
   - `httpx.Timeout(connect=60, read=30, write=30)` so a 30s no-byte gap raises
     `ReadTimeout`; plus an absolute ceiling (~25 min) via `asyncio.wait_for` so
     a trickle source can't hold a semaphore permit forever.
   - **Size cap (`DOWNLOAD_MAX_MB`):** if `Content-Length` already exceeds the
     cap, reject before the first byte. Otherwise keep a running byte total and
     abort the moment it exceeds the cap. On abort: close response, delete the
     partial file, job `blocked` "exceeds size cap".
   - Never trust the remote filename; the served name is rebuilt from the title.
3. Release the **download** semaphore as soon as bytes are on disk; verify+scan
   run under the separate `SCAN_CONCURRENCY` (so VT's slow polls don't starve
   downloads).

### 7.3 Format verify (`services/scanner.py`)

This runs **in the main process before VT** (you can't scan a file you haven't
confirmed is the right type). It is a **format sniff, not a security boundary** —
the boundaries are the converter sandbox (§8) and VT. Harden it against bombs:

- **PDF:** first 5 bytes `%PDF-` at offset 0. (For the *converter* input, also
  reject if the tail contains a ZIP EOCD signature `PK\x05\x06` — drops obvious
  PDF/ZIP polyglots before ghostscript sees them.)
- **EPUB:** open with `zipfile.ZipFile` (proves it's a real ZIP — drop the
  separate `PK\x03\x04` prefix check), then:
  - reject if `len(zf.infolist()) > 2000` (entry-flood DoS),
  - reject if `sum(zi.file_size)` (declared uncompressed) exceeds a cap
    (e.g. 1.5 GB) or the ratio to on-disk size is absurd (>100x) — zip-bomb guard,
  - assert `'mimetype'` is in `namelist()` and its bytes equal
    `b'application/epub+zip'`. Read **only** that one member.
  - Do **NOT** require mimetype-first/uncompressed (rejects valid Libgen EPUBs),
    and **NEVER** call `extractall()`/`extract()` to a directory (zip-slip).
- Mismatch → job `blocked` "not a real EPUB/PDF", delete file.

### 7.4 VirusTotal scan (`services/scanner.py`) — strict, block-all-unscanned

API v3, header `x-apikey`. Free tier: 4 req/min, **500/day**, 32 MB direct upload
(650 MB via the one-shot `GET /api/v3/files/upload_url` route).

> **Policy (decided): a file is served ONLY with a fresh "clean" verdict.
> Everything else is NOT served.** There is no `ALLOW_UNSCANNED_LARGE`. This is
> the whole point of the tool.

1. SHA-256 the file.
2. Check `vt_cache`. **Cache hit counts only if fresh:** `last_analysis_date`
   within `VT_CLEAN_MAX_AGE_DAYS` AND `engines_total >= VT_MIN_ENGINES`.
   Stale/thin clean entries are treated as a miss and re-scanned.
3. `GET /api/v3/files/{sha256}`:
   - **200:** read `last_analysis_stats`. Verdict:
     `malicious >= 1` → `malicious`; else `suspicious >= 2` → `suspicious`; else
     if (`last_analysis_date` fresh AND `engines_total >= VT_MIN_ENGINES`) →
     `clean`; else (known but stale/thin) → fall through to upload+poll like 404.
   - **404 (unknown):**
     - file ≤ 32 MB: `POST /api/v3/files`, then poll `GET /api/v3/analyses/{id}`
       every 20–30s (counts against quota — poll sparingly), hard cap
       `asyncio.wait_for` ~4 min. On `completed` → map stats as above. On
       timeout → `unverified` (reason `scan_timeout`).
     - file 32–650 MB: `GET /api/v3/files/upload_url` for a one-shot endpoint,
       `POST` the file there, then poll as above. Upload timeout and poll budget
       scale with size (~15 min cap), since big files queue longer at VT.
     - file > 650 MB: no upload route left → `unverified` (reason `too_large`).
4. Verdict handling:
   - `clean` → move `quarantine/ → ready/`, job `clean`, set
     `download_url = /api/files/{job_id}`. Cache verdict + freshness metadata.
   - `malicious` / `suspicious` → **delete file immediately**, job `blocked`,
     detail = detection counts. Cache verdict. Write an `events` block row
     (persist title/source/sha256/counts even though the file is gone).
   - `unverified` (too_large / scan_timeout / quota) → **delete file**, job
     `unverified`, carry the specific `reason`. Not served. For transient reasons
     (quota/timeout) the UI offers "retry later".
   > Note: with `DOWNLOAD_MAX_MB` (200) below VT's 650 MB upload limit, the
   > `too_large` path is unreachable by construction. It only opens up if
   > `DOWNLOAD_MAX_MB` is raised past 650, which would leave the 650–N MB band
   > permanently `unverified` (blocked) under this policy. Do not do that.
5. **Rate + quota control:** a token bucket (4/min) wraps **all** VT calls
   (lookups AND analysis polls) using `asyncio.sleep`. Track a daily counter
   (reset at UTC midnight); when it reaches `VT_DAILY_CAP`, stop calling VT and
   return `unverified` (reason `quota`) without hitting the API. On HTTP 429:
   wait 60s, retry once, then `unverified` (reason `quota`). Surface remaining
   quota on the admin Events view.

### 7.5 EPUB active-content warning (separate from the VT verdict)

VT is tuned for host malware; it will rate a perfectly valid EPUB "clean" even if
that EPUB carries live HTML/JS/SVG that can harm the **reader device** (JS,
remote-resource beacons, scriptable SVG, phishing redirects). The server never
opens these files, so this is a **client-side** risk — but the UI must not equate
"VT clean" with "safe to open".

At verify time (cheap, in-process, no extraction), inspect the EPUB zip listing
and flag a **warning chip** (distinct from the verdict) if it contains: `.js`
files, `<script>` in XHTML, remote `http(s)` references in content, or scriptable
SVG. Surface as "Contains active content — open in a reader that disables
scripts." This does **not** block the download; it informs.

### 7.6 Serve + delete (`routers/api_files.py`)

- `GET /api/files/{job_id}`: auth required.
  - Reject if `job_id` fails a strict UUID4 regex.
  - Look it up in the **in-memory registry** (sole source of truth); 404 if
    absent. Derive `ext` from the registry entry (never from the URL); assert
    `ext in {'epub','pdf'}`.
  - Build the path as `ready_dir / f"{job_id}.{ext}"`, `resolve()` it, assert it
    is inside `ready_dir` (defense in depth). User input never touches a path.
  - Add `job_id` to an **in-flight serves** set (under the registry lock) before
    returning the `FileResponse`; the TTL cleaner skips any path whose job_id is
    in that set (no delete-mid-serve race).
  - Support HTTP **Range** (FileResponse does) so interrupted mobile downloads
    resume.
  - **Deletion model:** primary = TTL cleaner. Delete-after-send is best-effort
    via a `BackgroundTask` that (a) is idempotent (`try/except FileNotFoundError`)
    and (b) removes the job_id from the in-flight set. Flip job status to
    `consumed` on first completed serve so the file dies on download OR on TTL,
    whichever first, but an interrupted download can still be retried from the
    same URL until TTL.
- `services/cleaner.py`: asyncio task from startup; every 5 min deletes anything
  in `quarantine/` or `ready/` older than `FILE_TTL_MINUTES` (skipping in-flight
  serves), and runs the DB pruning from §5.

---

## 8. PDF → EPUB converter (gVisor-sandboxed, no network)

> This is the RCE surface. An uploaded PDF is hostile code, not data. It is
> parsed by poppler/ghostscript/ocrmypdf/tesseract/Calibre — a toolchain with a
> long history of memory-corruption + ghostscript RCE CVEs. It runs in a
> **disposable gVisor worker container with no network and no access to app.db
> or .env.**

### 8.1 Flow

- `POST /api/convert` multipart, PDF only (`%PDF-` check + polyglot tail check
  §7.3), cap `CONVERT_MAX_MB`. The convert page collects **title + author**
  (don't derive title from a possibly-garbage filename). Creates a convert job
  (same registry/polling), `CONVERT_CONCURRENCY=1`.
- Write the upload to a fresh per-job scratch dir: `${DATA_DIR}/jobs/<job_id>/in.pdf`.
- Launch the worker (§8.2). Worker writes `out.epub` into the same scratch dir.
- On success move `out.epub` → `ready/`, served + deleted exactly like §7.6 (no
  VT — it's the user's own file).
- On every exit path (success/timeout/error) delete the scratch dir.

### 8.2 The worker (`Dockerfile.converter` + `converter/run_convert.py`)

A separate image with Calibre + ocrmypdf + tesseract + poppler. The app launches
it per job:

```
docker run --rm \
  --runtime=${RUNSC_RUNTIME} \          # gVisor: syscalls hit gVisor's userspace kernel
  --network=none \                       # NO network for the parsers
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --memory 1g --memory-swap 1g \
  --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=1g \
  -v ${DATA_DIR}/jobs/<job_id>:/work:rw \   # ONLY this job's dir, never /data root, never .env
  ${CONVERTER_IMAGE} \
  python /app/run_convert.py /work/in.pdf /work/out.epub \
    --title "<title>" --author "<author>" --ocr-langs "${OCR_LANGS}"
```

- The app **never** puts `VT_API_KEY`/`VK_TOKEN`/session secrets in the worker
  env, and bind-mounts **only** the single job's dir.
- Inside the worker, `run_convert.py` additionally sets `resource.setrlimit`
  (RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE, RLIMIT_NPROC) as a second cap.
- Env inside worker: `HOME=/tmp`, `TMPDIR=/tmp`, `CALIBRE_TEMP_DIR=/tmp`,
  `CALIBRE_CONFIG_DIRECTORY=/tmp/calibre`, `MPLCONFIGDIR=/tmp/mpl` (needed
  because `--read-only` blocks writes outside /tmp and /work).

### 8.3 Launching the worker without the raw docker socket

The app container must NOT mount `/var/run/docker.sock`. Use
`tecnativa/docker-socket-proxy` with a tight allowlist (only
`POST /containers/create`, `/start`, `/wait`, `DELETE /containers/{id}`, and an
image/label filter) on a dedicated network the app talks to. The app's docker
client points at the proxy. The proxy is the only thing that touches the real
socket.

### 8.4 Conversion logic (`converter/run_convert.py`)

1. **Text-density probe:** run `pdftotext` over evenly-spaced pages (not just
   first 5), compute words-per-page.
2. **OCR (ocrmypdf):** if density `< ~10 words/page` → the doc is scanned.
   Run `ocrmypdf --skip-text -l ${OCR_LANGS} in.pdf ocr.pdf`. `--skip-text` is a
   no-op on real text pages, so it's also safe to run on **mixed** docs below a
   higher threshold (image pages get OCR'd, text pages pass through). Offer
   `--redo-ocr` as an option for PDFs with a garbage existing text layer.
   OCR languages = `OCR_LANGS` (`eng+por+fra+spa`); the convert page may expose a
   language selector (default = all installed). Timeout `OCR_TIMEOUT_S`; on
   timeout, `kill` the **process group** (`start_new_session=True` +
   `os.killpg`). If OCR fails → fall back to converting the original PDF (don't
   fail the whole job).
3. **Convert:**
   ```
   ebook-convert in.pdf out.epub \
     --enable-heuristics \          # best-effort cleanup, overridable
     --unwrap-factor 0.5 \
     --authors "<author>" --language <lang> --title "<title>"
   ```
   - **REMOVE** `--pdf-engine=pdftohtml` — it is **not a real ebook-convert
     option** and exits non-zero (would crash 100% of conversions). Calibre has
     no PDF engine selector.
   - Timeout `CONVERT_TIMEOUT_S`; same process-group kill on timeout.
   - Capture stderr **tail** (cap size — Calibre is chatty) into the job detail;
     stream coarse progress (ocrmypdf prints per-page, Calibre prints phases) so
     the UI isn't frozen.
   - Optionally `--cover` from PDF page 1.
4. Non-zero return code → job `error` with stderr tail.

### 8.5 Honest UI note

The convert page states plainly: text-first PDFs (novels, single-column)
convert well; **tables and multi-column layouts are best-effort and degrade.**
This is inherent to PDF→EPUB, not a bug.

---

## 9. Auth + admin

- **Sessions = opaque tokens, no SECRET_KEY.** The cookie value *is* a random
  `secrets.token_urlsafe(32)` stored in the `sessions` table and validated by DB
  lookup. (Dropped signed cookies / SECRET_KEY entirely: a "generate if missing"
  key with no persistence silently logs everyone out on every restart. Opaque
  tokens are simpler and survive restarts.)
- `POST /api/auth/login` `{username,password}` → verify argon2 → insert session
  (30-day expiry) → set cookie `session=<token>; HttpOnly; SameSite=Lax; Path=/`
  (+ `Secure` when `COOKIE_SECURE=true`). If `must_change_password`, force the
  change form.
- `POST /api/auth/logout` (delete session + cookie), `POST /api/auth/change-password`.
- Dependency `require_user` on every `/api/*` except `login` and `health`. Pages
  other than `login`/`change-password` redirect to `/login` on invalid cookie.
- **Login rate limit:** 5 failures/min per key. Key = `request.client.host`
  direct, or validated `X-Forwarded-For` when behind a proxy (set uvicorn
  `--proxy-headers` + trusted forwarded-allow-ips). Best-effort; argon2 is the
  real defense. In-memory fixed-window dict, pruned periodically.
- **Admin panel** (`admin.html`, `require_admin`):
  - Users: list, create (username + password + admin flag), delete (not self),
    reset password (sets `must_change_password=1`).
  - **Events view:** read `events` table — blocked files (with counts), provider
    health, VT quota remaining, which mirror served each search. This is the
    actual operational value.
  - Set `VK_TOKEN` (stored in DB, overrides env).
  - `GET/POST/DELETE /api/admin/users…`, `GET /api/admin/events`,
    `GET /api/admin/providers`, `POST /api/admin/vk-token`.

---

## 10. Frontend (PWA)

Vanilla HTML/CSS/JS, dark theme, mobile-first. No framework.

- **login.html / change-password.html** — forms, error display.
- **index.html (Search)**
  - search box (Enter/button only — never per-keystroke), filter chips
    (`EPUB | PDF | both`), source checkboxes (only enabled providers).
  - per-provider status banner when a provider errored/timed out.
  - results table (deduped): title, author, size, ext badge, source badge(s),
    "Get" button. Same book from multiple sources = one row, multiple Get options.
  - clicking Get follows the job inline:
    `Queued… → Downloading… → Verifying… → Checking (VirusTotal)…` then
    `✅ Download` / `🚫 Blocked: detected by N engines` /
    `⚠️ Unverified: <reason>` (with "retry later" for quota/timeout).
    Show a distinct "Queued…" and "waiting in line" when blocked on a semaphore
    or the VT token bucket, so a slow phone user knows it isn't frozen.
  - clean EPUBs with active content show the §7.5 warning chip next to Download.
  - "Recent" section from `/api/history`.
- **convert.html** — file picker for one PDF + title/author inputs, progress
  states (`Uploading → OCR (if needed) → Converting → Download EPUB`), the §8.5
  honest note.
- **admin.html** — users + events + provider health + VK token.
- **manifest.webmanifest** — name/short_name "BookHub", `start_url '/'`,
  `scope '/'`, `display standalone`, dark `background_color`/`theme_color`,
  icons 192 + 512 + 512 `maskable`. In `index.html`: `apple-touch-icon` (180),
  `apple-mobile-web-app-capable`, status-bar-style.

### 10.1 Polling helper (`app.js`)

- Start at 1s; back off to 3–5s once status == `scanning` (VT is slow anyway).
- Pause on `document.hidden`; resume + immediately re-poll on `visibilitychange`.
- Stop on terminal status (`clean`/`blocked`/`unverified`/`error`/`consumed`).
- Treat 404 from `/api/jobs/{id}` as terminal "job lost (server restarted),
  retry", not an infinite loop.
- Absolute cap ~7 min → "taking too long, check later".
- `api()` wrapper: JSON, redirect to `/login` on 401, and distinguish a network
  `TypeError` ("Network unreachable") from HTTP errors.

### 10.2 Service worker (`sw.js`) — must not break the app

- Serve `sw.js` from `/sw.js` (route returns it with
  `Service-Worker-Allowed: /`) so it controls navigations.
- **First line of `fetch` handler:**
  `if (url.pathname.startsWith('/api') || request.method !== 'GET') return;`
  (let it hit the network; never `respondWith`). Explicitly excludes
  `/api/files/*` downloads and `/api/convert`.
- Static JS/HTML: **network-first** (or stale-while-revalidate), so a deploy's
  new contract is picked up — NOT pure cache-first (which serves stale JS after
  deploys even with a version bump).
- `install` → `skipWaiting()`; `activate` → `clients.claim()` + delete caches
  whose key != current version. Bump the version constant on every deploy.
- Offline: listen to `online`/`offline`, show a persistent "Offline — search and
  downloads need a connection" banner, disable Search/Get/Convert while offline.
  Do not cache search results.

---

## 11. Docker

### 11.1 `Dockerfile` (main app)

- base `python:3.12-slim` (pin by digest),
- install only what the **app** needs (httpx, fastapi, etc.) + the docker CLI/
  SDK to talk to the socket-proxy. **Calibre/ocrmypdf/tesseract live in the
  converter image, NOT here**, so a compromise of the app image has a smaller
  toolchain.
- non-root user uid 10001, owns `/data`,
- `HOME=/tmp` etc. (read-only rootfs),
- `pip install` from pinned `requirements.txt`,
- `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--proxy-headers"]`.

### 11.2 `Dockerfile.converter`

- base `python:3.12-slim`, install `calibre poppler-utils ocrmypdf
  tesseract-ocr-eng tesseract-ocr-por tesseract-ocr-fra tesseract-ocr-spa`
  (ghostscript comes via ocrmypdf — ensure a recent version, `-dSAFER` default),
- non-root, entrypoint `python /app/run_convert.py`,
- this image is launched per job with the flags in §8.2.

### 11.3 `docker-compose.yml`

```yaml
networks:
  bookhub_net:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.name: br-bookhub
  proxy_net:        # only reverse proxy + app
  socketproxy_net:  # only app + docker-socket-proxy

services:
  bookhub:
    build: { context: ., dockerfile: Dockerfile }
    user: "10001:10001"
    read_only: true
    cap_drop: [ALL]
    security_opt:
      - no-new-privileges:true
      - seccomp=default            # never unconfined
    pids_limit: 256
    mem_limit: 2g
    memswap_limit: 2g
    cpus: "2.0"
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=512m
    volumes:
      - ./data:/data:rw            # only writable persistent path; app.db + quarantine + ready + jobs
    ports: ["127.0.0.1:8000:8000"] # loopback only; reverse proxy / Tailscale fronts it
    env_file: .env
    networks: [bookhub_net, proxy_net, socketproxy_net]
    healthcheck:
      test: ["CMD","curl","-fsS","http://localhost:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s
    restart: unless-stopped

  dockerproxy:
    image: tecnativa/docker-socket-proxy
    environment:
      CONTAINERS: 1
      POST: 1                       # allow create/start
      # everything else default-deny; tighten to the minimal verbs needed
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [socketproxy_net]
    restart: unless-stopped

  # Option A access (recommended): cloudflared sidecar. Outbound 443 only;
  # reaches the app at http://bookhub:8000 over proxy_net. The app exposes no
  # VLAN port. Omit this service if using Option B (existing services tunnel).
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    environment:
      - TUNNEL_TOKEN=${CLOUDFLARED_TOKEN}   # from .env; tunnel created in CF dashboard
    networks: [proxy_net]
    restart: unless-stopped
    # ingress (hostname -> http://bookhub:8000) configured in the CF dashboard,
    # and a Cloudflare Access policy is applied to that hostname.
```

For **Option A**, the app's published port can be dropped entirely (cloudflared
reaches it over `proxy_net`); change `ports: ["127.0.0.1:8000:8000"]` to no
published port and let `cloudflared` talk to `bookhub:8000` internally. Add
`CLOUDFLARED_TOKEN=` to `.env`/`.env.example`.

- The host's Docker daemon must register the **`runsc` runtime**
  (`/etc/docker/daemon.json`) so the converter worker can run under gVisor.
- `read_only: true` is the change most likely to break Calibre — but Calibre is
  in the *worker*, not the app, so the app being read-only is safe. Verify the
  app boots and serves after enabling it.
- Worker containers are `--rm` and not declared here; the app launches them via
  `dockerproxy` per §8.

---

## 12. Observability (`app/events.py`)

- **Structured logs** (JSON lines) for: search (query, providers, mirror),
  download start, verdict (sha256, source, title, malicious/suspicious counts),
  block reason, serve, TTL sweep, VT 429/quota, provider disable. **No secrets.**
- Mirror those key events into the `events` table (§5) so the admin Events view
  shows blocked files (persisted even after the file is deleted), provider
  health, and VT quota remaining. This is the product's real value beyond raw
  download.

---

## 13. SQLite / async contract (pin this — don't reach for the blocking default)

- **`aiosqlite` consistently** (truly async; never mix in blocking stdlib
  `sqlite3` inside handlers — it stalls the event loop, freezing the very polling
  endpoints the UI depends on).
- On every connection open: `PRAGMA journal_mode=WAL`, `busy_timeout=5000`,
  `synchronous=NORMAL`.
- **One writer:** SQLite allows a single writer. Serialize all writes through one
  shared connection or an `asyncio.Lock` around writes.
- `vt_cache`, `history`, `users` writes use `INSERT … ON CONFLICT … DO UPDATE`
  (idempotent).
- **No connection pool, no async ORM** — overkill for one SQLite file. One WAL
  connection (or a tiny per-request connection) is the correct ceiling.

---

## 14. Milestones (implement in this order, commit per milestone)

1. **Skeleton** — repo layout, `config.py`, `Dockerfile`/`Dockerfile.converter`/
   compose (hardened), FastAPI boots, static pages served, `GET /api/health`
   (no auth). DB init with WAL pragmas + schema (§5, §13).
2. **Auth** — opaque-token sessions, argon2, admin bootstrap (`must_change_password`
   column), admin user CRUD, login rate limit. Tests: login ok/fail, admin guard,
   non-admin blocked, must-change flow.
3. **Libgen provider (.li)** — mirror probe + demotion, **`index.php` parser by
   table id + header names** (build this FIRST — `search.php` is dead),
   Cloudflare-challenge detection, `resolve` with the ads.php→key→CDN handshake
   returning a `DownloadPlan`. Tests against **committed HTML fixtures**, not live
   calls.
4. **Job pipeline** — downloader (stall guard, size cap, semaphore release),
   format verify (bomb guards, real-zip EPUB check, polyglot tail check), VT scan
   (freshness floor, block-all-unscanned, token bucket + daily quota, unverified
   reasons), EPUB active-content warning, serve+delete (in-flight set, UUID4
   guard, Range, consumed status), TTL cleaner + pruning. Tests: magic-byte cases
   (real pdf, real epub, exe-renamed, zip-bomb-ish fixture), VT verdict mapping +
   freshness + quota with mocked httpx, cache hit/stale path, path-traversal
   rejection.
5. **Search UI** — wire index.html (search + dedup display + per-provider banner
   + job polling with backoff/visibility + warning chip + recent).
6. **Converter** — `dockerproxy`, converter image, `run_convert.py`
   (density probe, OCR multi-lang, no fake flag, timeouts + process-group kill,
   rlimits), app orchestration launching the **gVisor `--network=none` worker**,
   convert page (title/author inputs + honest note). Tests: tiny text-PDF fixture
   converts; density/OCR-gate logic unit-tested (mock subprocess); worker launch
   args asserted.
7. **VK + Anna's Archive providers** — VK (`docs.search`, error 5/15 self-disable,
   admin token override) and AA (donor-key JSON path + HTML fallback). Dedup
   across all three. README token guides.
8. **Observability + admin events** — structured logs + events table + admin
   Events/provider-health/VT-quota views.
9. **PWA polish** — manifest, network-first service worker (with the `/api`
   guard), icons (incl. maskable + apple-touch), offline UX, mobile CSS.
10. **README.md** — host/VLAN/egress setup (§3), gVisor runtime install, `.env`,
    VT key steps, VK token steps, AA donor key, reverse-proxy/Tailscale guidance,
    backup/restore, upgrade path, legal note.

Definition of done per milestone: code + tests pass (`pytest`) + container builds
+ the feature works through the browser.

---

## 15. Security checklist (verify at the end, all must hold)

- [ ] No `/api` route reachable without a valid session (except `login`/`health`).
- [ ] Admin routes reject non-admin sessions.
- [ ] Passwords argon2-hashed; no plaintext anywhere, including logs.
- [ ] **Converter runs in a gVisor (`runsc`) worker with `--network=none`,
      `--read-only`, `--cap-drop ALL`, only the single job dir mounted, and NO
      VT/VK/session secrets in its env.**
- [ ] App container never mounts the raw docker socket; only the scoped
      socket-proxy does. No `privileged`/`network_mode:host`/`pid:host`/
      `seccomp=unconfined` anywhere.
- [ ] Host on its own isolated VLAN; OPNsense denies RFC1918 + management egress,
      allows only internet to the sources + VT.
- [ ] Downloaded files: format-verified (bomb-guarded, no extractall) before VT;
      **served only with a fresh VT "clean"**; malicious/suspicious deleted;
      unverified (too-large/quota/timeout/stale) deleted and not served.
- [ ] Served filenames sanitized (`safe_filename`: keep `[A-Za-z0-9 ._-]`, ≤120).
- [ ] Job IDs are UUID4; file paths always `dir / f"{job_id}.{ext}"` with a
      `resolve()`-inside-dir assertion; user input never touches a path; unknown
      job_id → 404 (fail closed on restart, no FS glob).
- [ ] Size caps enforced during streaming (download AND upload), partials cleaned.
- [ ] App container non-root, read-only rootfs, mem/pids/cpu limits, loopback bind.
- [ ] Service worker never caches `/api`, `/api/files/*`, or `/api/convert`;
      network-first for shell.
- [ ] VT key, VK token, and all secrets only in `.env`/DB, never sent to frontend.
- [ ] EPUB active-content warning shown but never equated with "VT clean".

---

## 16. Out of scope for v1 (do not build)

- Sources beyond VK + Anna's Archive + Libgen .li (add later via the protocol).
- Email, password reset flows, 2FA.
- Persistent job queue / Redis / Celery — in-memory is fine.
- EPUB→PDF or any other conversion direction.
- Multi-language UI (English only).
- Kindle "send to device" integration.
- FlareSolverr / headless-browser Cloudflare bypass.
- Marker / pdf2htmlEX layout-accurate conversion (see §13 note below).

---

## 17. Deliberately rejected alternatives (so choices are intentional, not silent)

- **Marker (ML PDF→EPUB):** does tables/layout far better, but too heavy/slow for
  a CPU homelab box. Flagged as a future opt-in for table-heavy PDFs.
- **pdf2htmlEX:** produces pixel-accurate but **non-reflowable** output, wrong for
  e-readers.
- **SSE/WebSocket job updates:** over-engineered for single-instance low traffic;
  polling with backoff is correct.
- **Signed-cookie sessions / SECRET_KEY:** rejected for opaque DB tokens (survive
  restarts, simpler).
- **LXC host:** rejected — shares the Proxmox kernel and can't run gVisor cleanly.

---

## 18. Known risks (accepted, handle gracefully)

- Libgen mirrors/DOM churn → parsers fail soft (rotate + log), never crash search.
- Cloudflare may start challenging the working mirrors → detect + rotate + surface;
  if all mirrors challenge, VK + Anna's Archive carry the search.
- VK `docs.search` may be denied (error 15) for some tokens → self-disable + admin
  visibility.
- VT free quota (500/day) can exhaust → strict policy means affected files become
  `unverified` (not served), with "retry later"; admin sees quota remaining.
- Strict block-all-unscanned + VT's 650 MB upload limit → books larger than that
  cannot be VT-verified and won't download. Accepted cost of strict mode.
- Every Libgen mirror hands out a `get.php` link that redirects to the same CDN
  host per md5, so one dead host kills the whole candidate list. When that
  happens the job retries the same title on archive.org (near-exact title match
  only). A title that exists nowhere else stays unavailable until the CDN
  recovers — nothing in our control.
- Calibre PDF→EPUB quality varies on complex layouts — communicated in UI, not
  solvable for free on CPU.
- gVisor adds a small per-conversion startup cost and needs the `runsc` runtime on
  the host — documented in README.
