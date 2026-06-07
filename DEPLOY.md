# BookHub Deployment (Homelab: OPNsense + Proxmox + Cloudflare Tunnel)

Target: a dedicated, isolated **VLAN 80** with its own Proxmox VM, reachable
from the internet only through a Cloudflare Tunnel (no inbound firewall hole).

> Subnets below are placeholders. Match your own scheme. VLAN 80 is assumed
> `10.0.80.0/24` with the OPNsense gateway at `10.0.80.1`.

Existing VLAN map (for context):

| VLAN | Purpose |
|------|---------|
| 100  | Management (Proxmox, OPNsense, Switch, APs) |
| 10   | Office |
| 20   | Gaming |
| 30   | Services (CasaOS, Nextcloud, Immich) |
| 40   | Main Wi-Fi |
| 50   | Guest Wi-Fi |
| 60   | IoT |
| 70   | DMZ Web |
| **80** | **BookHub (new, isolated)** |

Why a new VLAN and not Services (30): BookHub fetches untrusted files from the
internet and runs a converter that parses untrusted PDFs. Keep it off the same
L2 as Immich/Nextcloud so a compromise cannot move laterally to your data.

---

## 1. OPNsense: create VLAN 80

1. Interfaces → Other Types → VLAN → **Add**.
   - Parent interface: your trunk NIC to the switch.
   - VLAN tag: `80`.
   - Description: `BookHub`.
2. Interfaces → Assignments → add the new VLAN interface → **Save**.
3. Click the new interface → **Enable**.
   - IPv4 Configuration Type: **Static IPv4**.
   - Address: `10.0.80.1/24` (this is the VLAN gateway).
   - **Save**, then **Apply**.

## 2. Switch (Zyxel, standalone)

- Tag VLAN 80 on the port to OPNsense (trunk).
- Tag VLAN 80 on the port to the Proxmox host (trunk).
- Both uplinks carry VLAN 80 tagged.

## 3. DHCP (or static lease)

- Services → DHCPv4 → [VLAN80 interface].
- Enable. Range `10.0.80.100 – 10.0.80.200`.
- Recommended: give the BookHub VM a static mapping (fixed IP).

## 4. Firewall rules (VLAN80 interface tab) — ORDER MATTERS

This is the isolation core. Rules are evaluated top to bottom.

| # | Action | Source | Destination | Port | Purpose |
|---|--------|--------|-------------|------|---------|
| 1 | Pass   | VLAN80 net | VLAN80 address | 53 (TCP/UDP) | DNS to firewall |
| 2 | Block  | VLAN80 net | alias `RFC1918` (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) | * | Block all internal VLANs (incl. Immich) |
| 3 | Pass   | VLAN80 net | any | * | Internet egress (downloads, VirusTotal, Cloudflare) |

Result: the VM reaches the internet and DNS, and nothing else internal.

> Create the `RFC1918` alias under Firewall → Aliases if you do not have one.
> Also add `169.254.0.0/16` to the block if you want to be thorough.

## 5. Proxmox: create the VM

- Create VM (NOT LXC — gVisor needs a real kernel).
  - OS: **Ubuntu Server 24.04 LTS** (or 22.04). Debian 12 also fine.
  - Use **Server**, not Desktop. Minimal install, no GUI.
  - CPU: 2 vCPU. RAM: 4 GB (2 GB bare minimum). Disk: 30 GB (20 GB minimum).
  - **Network**: bridge `vmbr0`, **VLAN Tag = 80**.

- Boot, install OS, then install Docker from the **official repo** (NOT snap):

  ```bash
  # Ubuntu ships a snap Docker that breaks bind mounts and the socket-proxy.
  # Remove it if present, then use the official convenience script.
  sudo snap remove docker 2>/dev/null || true
  sudo apt-get remove -y docker.io docker-doc docker-compose podman-docker containerd runc 2>/dev/null || true

  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER   # re-login after
  ```

- Confirm it is the real engine, not snap:

  ```bash
  which docker            # must be /usr/bin/docker, NOT /snap/bin/docker
  docker compose version  # compose plugin present
  ```

- Set a static IP (or rely on the DHCP reservation from step 3).

- For the converter (gVisor), install runsc — see BUILD.md §8:

  ```bash
  # gVisor (runsc), Ubuntu/Debian apt method:
  sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
  sudo apt-get update && sudo apt-get install -y runsc

  # Register the runtime with Docker, then restart:
  sudo runsc install
  sudo systemctl restart docker
  docker info | grep -i runtimes   # should list runsc
  ```

> Ubuntu note: if AppArmor blocks runsc, it is usually harmless for our
> `--network=none --read-only` worker. If a container fails to start, check
> `journalctl -u docker` and `dmesg | grep -i apparmor`.

## 6. Deploy BookHub on the VM

```bash
git clone <your-repo> bookhub && cd bookhub
cp .env.example .env
# Edit .env (see below), then:
docker compose build
docker compose --profile cloudflared up -d
```

Key `.env` values for production:

```
DATA_DIR=/data
HOST_DATA_DIR=/opt/bookhub/data   # absolute HOST path that maps to /data
COOKIE_SECURE=true                # TLS terminates at Cloudflare
ADMIN_PASSWORD=<a strong >=12 char password>
VT_API_KEY=<your VirusTotal key>
RUNSC_RUNTIME=runsc               # gVisor in production
CONVERTER_IMAGE=bookhub-converter:latest
CLOUDFLARED_TOKEN=<from step 7>
```

> `HOST_DATA_DIR` MUST match the host path bind-mounted to `/data` in
> docker-compose.yml. The converter launches sibling containers via the
> docker-socket-proxy, and the daemon resolves bind mounts on the HOST.

Build the converter image (used by the converter worker):

```bash
docker build -f Dockerfile.converter -t bookhub-converter:latest .
```

## 7. Cloudflare Tunnel (Option A — sidecar, no inbound)

In the Cloudflare dashboard:

1. Zero Trust → Networks → Tunnels → **Create a tunnel** → Cloudflared.
2. Name it `bookhub`. Save. **Copy the tunnel token.**
3. Public Hostname:
   - Subdomain/host: `bookhub.yourdomain.com`
   - Service: `http://bookhub:8000` (the compose service name on `proxy_net`).

On the VM:

4. Put the token in `.env`: `CLOUDFLARED_TOKEN=<token>`.
5. `docker compose --profile cloudflared up -d`.

The cloudflared sidecar dials OUT on 443 only. No inbound port, no NAT, no
firewall hole. VLAN80 firewall rule #3 (internet egress) is all it needs.

## 8. Cloudflare Access (strongly recommended)

1. Zero Trust → Access → Applications → **Add an application** (Self-hosted).
2. Domain: `bookhub.yourdomain.com`.
3. Policy: Allow → your email (One-time PIN or your IdP).

Now the app is private: even with the public URL, only you can authenticate at
Cloudflare's edge before reaching BookHub's own login.

## 9. Sanity checks

```bash
docker compose ps                       # all healthy
curl -fsS http://localhost:8000/api/health   # {"status":"ok"} (on the VM)
docker info | grep -i runtimes          # runsc present
```

- Browse to `https://bookhub.yourdomain.com` → Cloudflare Access → BookHub login.
- Search, download a book → clean → download.
- Convert page → upload a PDF → EPUB (uses the gVisor worker).
