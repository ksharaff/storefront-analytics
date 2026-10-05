# Deployment runbook: one VPS, Cloudflare Tunnel, and email login

This is how the dashboard runs in production: one small Linux server running
the Docker Compose stack in this folder. People open it at a subdomain you own
and log in with a one-time PIN that is emailed to them. Only the email
addresses you list can get in.

```
browser ──https──▶ Cloudflare Access (emailed PIN) ──▶ Cloudflare Tunnel
                                                          │ (outbound from the server)
VPS ───────────────────────────────────────────────────────┼──────────────
  cloudflared ──▶ app (dashboard + API) ──▶ db (PostgreSQL) ◀── sync (every 3 min)
                                                 ▲    ▲──────── catalog (daily)
                                                 └──────────── backup (nightly → ./backups)
```

The stack opens no public ports. The server's firewall allows SSH only.
Cloudflare terminates HTTPS, so there are no certificates to manage.

**Running costs:** about $10 a year for the domain and $5–20 a month for the
VPS. Cloudflare Tunnel and Access (email PIN login) are free on the Zero Trust
Free plan, which covers up to 50 users.

---

## 0. Decide where the server lives

- **Size:** 2 vCPU, 2 GB RAM, 20 GB or more of disk, running Ubuntu 24.04 LTS.
  The database is small (a few hundred MB for a year of orders), so this size
  is plenty.
- **Region:** the database holds customers' billing email addresses. These
  are personal data under Saudi Arabia's PDPL, which restricts transfers
  outside the Kingdom. A server in Saudi Arabia keeps this simple. As of
  September 2026, Oracle Cloud has Riyadh and Jeddah regions. AWS and Azure
  have announced Saudi regions for late 2026. Before choosing a region outside
  the Kingdom, check with the company. This is not legal advice.

## 1. Domain and Cloudflare (one-time, about 15 minutes)

1. Create a free Cloudflare account. Then either buy a domain under
   **Domain Registration** (sold at cost, about $10 a year for a .com) or add
   a domain the company already owns and switch its nameservers to Cloudflare.
2. **Set up the login before setting up the tunnel**, so the dashboard is
   never reachable without it:
   Zero Trust → **Access → Applications → Add an application → Self-hosted**.
   - Application domain: the subdomain you will use, e.g. `dash.example.com`
   - Policy: **Allow**, Include → **Emails**. List each person who should have
     access.
   - Login methods: **One-time PIN** (on by default).
3. Zero Trust → **Networks → Tunnels → Create a tunnel → Cloudflared**, and
   give it a name such as `storefront-analytics`.
   - On the install page, copy the **token** (the long string after `--token`
     in the `docker run` command). It goes into `.env` as `TUNNEL_TOKEN`.
     Treat it as a password.
   - Public hostname: `dash.example.com`, service **HTTP**, URL **`app:8000`**.
     `app` is the dashboard container's name on the Docker network.

## 2. Prepare the server (one-time)

Log in with an SSH key, as a normal user with sudo rather than as root.

```bash
# Updates, and automatic security updates from now on
sudo apt update && sudo apt upgrade -y
sudo apt install -y unattended-upgrades git

# Firewall: SSH only. The stack's single published port is bound to
# 127.0.0.1, so Docker's habit of bypassing ufw does not expose anything.
sudo ufw allow OpenSSH
sudo ufw enable

# Docker Engine + Compose plugin (Docker's official install script)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"    # then log out and back in
```

Get the code. The repository is private, so give the server a **read-only
deploy key**:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/storefront_deploy -N ""
cat ~/.ssh/storefront_deploy.pub
# GitHub → YOUR-USER/storefront-analytics → Settings → Deploy keys → Add
# (leave "Allow write access" unticked)

GIT_SSH_COMMAND="ssh -i ~/.ssh/storefront_deploy" \
  git clone git@github.com:ksharaff/storefront-analytics.git
cd storefront-analytics
git config core.sshCommand "ssh -i ~/.ssh/storefront_deploy"   # so later pulls use the key
```

Create the settings file:

```bash
cd ~/storefront-analytics/deploy
cp .env.example .env
openssl rand -hex 24          # paste this as POSTGRES_PASSWORD
nano .env                     # WooCommerce key/secret, password, TUNNEL_TOKEN
chmod 600 .env
mkdir -p backups
```

## 3. Move the data across (one-time)

The development PC already has the full 2026 backfill. Copying it takes a
minute, whereas re-running the backfill against the slow store would take
hours. The sync watermark comes along with the data, so the server's sync
continues from where the PC left off.

**On the PC (PowerShell),** from the repo folder, with the development database
running:

```powershell
docker exec storefront-analytics-db pg_dump -U storefront -Fc -f /tmp/storefront.dump storefront_analytics
docker cp storefront-analytics-db:/tmp/storefront.dump .\storefront.dump
scp .\storefront.dump USER@SERVER:~/storefront-analytics/deploy/backups/
Remove-Item .\storefront.dump     # it holds customer emails, so don't leave copies around
```

These commands have `pg_dump` write the file inside the container and then
copy it out. Don't use `pg_dump ... > file` in PowerShell: that redirection
re-encodes the output and corrupts the dump.

**On the server:**

```bash
cd ~/storefront-analytics/deploy
docker compose up -d db                        # the database only, for now
docker compose run --rm --entrypoint sh backup -c \
  'pg_restore --clean --if-exists --no-owner -d "$PGDATABASE" /backups/storefront.dump'
docker compose exec db psql -U storefront -d storefront_analytics \
  -c "SELECT count(*) AS orders FROM orders"   # should match the PC
rm backups/storefront.dump
```

Messages that `pg_restore` prints about dropping objects are expected. The
database starts out holding the empty schema, and `--clean` replaces it.

## 4. Start everything

```bash
docker compose up -d --build
docker compose ps                       # all "running"; app shows "(healthy)"
curl -s localhost:8000/api/health       # {"status":"ok", ... "orders": ...}
docker compose logs -f sync             # a line every 3 minutes; Ctrl+C to stop watching
```

Open `https://dash.example.com`. Cloudflare asks for your email, sends a PIN,
and then shows the dashboard.

Once this works, **stop running the sync on the PC**. The server is now the
live copy. The PC database is fine to keep for development.

---

## Day to day

All commands run from `~/storefront-analytics/deploy`.

| Task | Command |
|---|---|
| Deploy a new version | `git pull && docker compose up -d --build` |
| Is everything up? | `docker compose ps` |
| Sync log (one line per run) | `docker compose logs --tail 50 sync` |
| Dashboard / API errors | `docker compose logs --tail 100 app` |
| Restart one piece | `docker compose restart sync` |
| Stop / start everything | `docker compose stop` / `docker compose up -d` |
| Change a setting in `.env` | edit it, then `docker compose up -d` (recreates what changed) |
| Back up right now | `docker compose run --rm backup once` |
| Read delivery companies and times from OTO | `docker compose run --rm app python -m scripts.sync_oto_tracking` |
| Give or remove access | Cloudflare Zero Trust → Access → Applications → the policy's email list |

The data lives in the `pgdata` Docker volume. It survives restarts, rebuilds
and reboots. **`docker compose down -v` deletes it.** Never use `-v` here.

### Backups

- Every night at 03:00 Riyadh time, a backup is written to
  `deploy/backups/storefront_analytics_YYYYMMDD_HHMMSS.dump`. The last 14 days are
  kept (set by `BACKUP_KEEP_DAYS`).
- These copies are **on the same server**, so they protect against mistakes
  but not against losing the server. At least weekly, copy the newest one off
  the server, for example from the PC:
  `scp USER@SERVER:~/storefront-analytics/deploy/backups/storefront_analytics_2026MMDD_*.dump .`
  Your VPS provider's snapshot feature also works.
- The dumps contain customer emails. Store the copies somewhere private.

**Restoring a backup:**

```bash
docker compose stop app sync catalog
docker compose run --rm --entrypoint sh backup -c \
  'pg_restore --clean --if-exists --no-owner -d "$PGDATABASE" /backups/storefront_analytics_YYYYMMDD_HHMMSS.dump'
docker compose up -d
```

The sync then catches up on everything that changed since the backup. The
watermark is restored along with the data.

### Changing the status groups

`db/02_seed_status_groups.sql` runs only when the database is first created.
To change a status's group on the live server, run the `UPDATE` directly:

```bash
docker compose exec db psql -U storefront -d storefront_analytics \
  -c "UPDATE order_status_groups SET status_group = 'other' WHERE status = 'custom-refunded'"
```

Also update the seed file in git, so a fresh install gets the same result.

### Storage app and Warehouse tables

The storage app (the dashboard's **Storage** tab) and the dashboard's
**Warehouse** tab keep their own tables:

| File | Tables |
|---|---|
| `db/03_storage.sql` | `packed_orders` |
| `db/05_workers.sql` | `pick_workers`, `order_workers` |
| `db/06_unit_labels.sql` | `unit_labels` (one barcode label per piece) |
| `db/07_order_bol.sql` | `order_bol` (BOL, boxes, carrier from the OTO sync) |
| `db/08_shipping.sql` | `box_loads`, `packed_orders.shipped_at` (loading onto the truck) |
| `db/09_oto_tracking.sql` | `oto_orders` (delivery companies and delivery time from OTO; replaces the first version's `oto_tracking`) |

A database created after these files existed already has them. On a server
created before, run the missing ones once, in order (all are safe to re-run):

```bash
for f in 03_storage 05_workers 06_unit_labels 07_order_bol 08_shipping 09_oto_tracking; do
  docker compose exec db psql -U storefront -d storefront_analytics -f /docker-entrypoint-initdb.d/$f.sql
done
```

Then `docker compose up -d --build app` so the dashboard serves the new code.
Until they are run, the storage screens fail and the Warehouse tab says a
table is missing.

### OTO (tryoto.com)

Put `OTO_REFRESH_TOKEN` in `.env` (see README "Connecting OTO"); without it
the storage app's OTO sync runs in TEST mode. The token can do everything in
the OTO account, so treat it as a password; the app only ever reads from OTO.

The Warehouse tab's delivery companies and delivery time come from
`scripts/sync_oto_tracking.py`, which is **not scheduled in this stack yet**.
Run it by hand, daily (OTO only shows the last 90 days, so never leave it for
more than a month; what was read is kept):

```bash
docker compose run --rm app python -m scripts.sync_oto_tracking
```

### If something is wrong

- **Dashboard says data is stale:** check `docker compose logs --tail 20 sync`.
  "fetch failed" means the store or its API is unreachable. The sync keeps
  retrying and loses nothing, because the watermark only moves after a clean
  run.
- **Cloudflare error 1033 / "tunnel not connected":** run
  `docker compose logs cloudflared`. The usual causes are a wrong
  `TUNNEL_TOKEN`, or `app` not being healthy yet (cloudflared waits for it).
- **Cloudflare 502:** the tunnel's public hostname must point to `app:8000`
  over HTTP, not HTTPS and not localhost.
- **Out of disk:** run `docker system prune` to remove old images. This does
  not touch volumes.

---

## Optional: rehearse on the PC first

Docker Desktop can run the same stack locally, minus the tunnel. This checks
the image, the migration and the sync without a server:

```powershell
cd deploy
Copy-Item .env.example .env      # fill in the real WooCommerce values; set APP_PORT=8010
docker compose up -d --build db app sync catalog backup
# optional: rehearse step 3's restore here too
# open http://localhost:8010
docker compose down -v           # removes the rehearsal and its data (not your dev database)
```

The rehearsal runs as a separate Compose project (`storefront-analytics-prod`), so
it never touches the development database on port 5434.
