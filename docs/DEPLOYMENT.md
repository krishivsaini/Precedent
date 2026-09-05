# Deployment

Two halves, deployed independently.

| | What it is | Where it runs | What it costs |
|---|---|---|---|
| **Showcase** — `site/` | One static page, generated from `evals/results/` | Cloudflare Pages | free |
| **API + approval screen** — `src/precedent/api/` | FastAPI, SQLite, Razorpay webhook receiver | Google Cloud Run | ~$10–15/mo |

The split is not arbitrary. The showcase must load with no server, no database, and no cold
start, and every figure on it is read at build time from the JSON the eval harness wrote — so
it stays true whether or not a backend is up. The approval screen is the opposite: it is the
running system, it writes, and what it writes is the corpus.

---

## The one constraint that shapes everything

The store is a SQLite file. **SQLite takes one writer, so the service runs one instance** —
`--max-instances=1`, one uvicorn worker. That is not a knob to turn up when traffic grows.
Going wider is a database migration, not a flag, and this deployment is arranged so that
running out of headroom fails loudly rather than corrupting a corpus quietly.

Durability comes from [Litestream](https://litestream.io), which replicates the file to a
versioned GCS bucket continuously. The container filesystem stays disposable.

---

## Frontend — Cloudflare Pages

The build has **no dependencies at all**: `scripts/build_site.py` is standard library only.

### Dashboard (git integration)

Cloudflare Pages → Create project → connect the repo, then:

| Setting | Value |
|---|---|
| Build command | `python3 scripts/build_site.py` |
| Build output directory | `site` |
| Environment variable | `PRECEDENT_APP_URL` = your Cloud Run URL *(optional)* |

`PRECEDENT_APP_URL` adds the "the running approval screen →" link to the masthead. Leave it
unset and the link simply does not render — the page has no dead links to a torn-down demo.

### From your machine

```sh
python3 scripts/build_site.py
npx wrangler pages deploy          # project name and output dir come from wrangler.toml
```

The build also writes `site/_headers`, which gives Pages a CSP with the inline script pinned
by SHA-256. The hash is computed from the script it just generated, so it cannot drift.

---

## Backend — Google Cloud Run

### Once

```sh
gcloud auth login
set -a; . ./.env; set +a          # so the Razorpay secrets get uploaded on this first run
PROJECT_ID=your-project ./deploy/cloudrun.sh
```

The script is idempotent — it is also the redeploy command. It enables the APIs, creates the
versioned bucket, creates a runtime service account scoped to that one bucket, uploads the
three Razorpay secrets to Secret Manager, builds the image with Cloud Build, and deploys.

Re-running **without** the env vars exported leaves existing secret versions untouched, so a
routine redeploy can never blank a live secret.

| Variable | Default | |
|---|---|---|
| `PROJECT_ID` | *(required)* | |
| `REGION` | `asia-south1` | Mumbai — the payments this reconciles are INR |
| `MIN_INSTANCES` | `1` | see the cost note below |
| `SEED_ON_EMPTY` | `1` | seed the demo corpus on a genuinely empty database |

### Then point Razorpay at it

Dashboard → Settings → Webhooks → add `https://<service-url>/webhooks/razorpay`, with the
same secret stored in `razorpay-webhook-secret`, subscribed to `payment.captured` and
`refund.processed`.

The endpoint acks 200 on everything once a delivery is durably recorded, including a
signature failure — Razorpay retries on any non-2xx, and retry-storming yourself over a bad
signature is worse than recording it and moving on. Failures are stored with
`signature_valid=False` and never trusted downstream.

### About the cost

`--min-instances=1 --no-cpu-throttling` is what makes "the data is durable" a true statement.
Cloud Run's default is to give a container no CPU between requests, and a background
replicator that never runs is not a backup.

You can set `MIN_INSTANCES=0` and drop to roughly free. Litestream still does a final sync on
the SIGTERM that Cloud Run sends before it scales to zero, so the realistic exposure is small
— but it is no longer nil, and an instance killed without a clean SIGTERM loses whatever it
had not replicated. That is a fine trade for a demo and a bad one for anything real.

---

## Running the image anywhere

The same image serves every environment; the entrypoint branches on what is configured.

```sh
docker build -t precedent .

# Local: no replication, seeded demo corpus, database inside the container.
docker run -p 8080:8080 \
  -e PRECEDENT_SEED_ON_EMPTY=1 \
  -e RAZORPAY_KEY_ID=... -e RAZORPAY_KEY_SECRET=... -e RAZORPAY_WEBHOOK_SECRET=... \
  precedent

# A VM with a real disk: no replication needed, the volume is the durability.
docker run -p 8080:8080 -v /srv/precedent:/data precedent

# Replicated, to anything Litestream speaks (gcs://, s3://, file://).
docker run -p 8080:8080 -e LITESTREAM_REPLICA_URL=s3://bucket/precedent precedent
```

| Variable | Default | |
|---|---|---|
| `PRECEDENT_DB_PATH` | `/data/precedent.db` | also read by `scripts/seed_demo.py`, so seeding cannot populate a different file from the one served |
| `LITESTREAM_REPLICA_URL` | *(unset)* | unset ⇒ no replication, and the entrypoint runs uvicorn directly |
| `PRECEDENT_SEED_ON_EMPTY` | `0` | only ever acts on a genuinely absent database |
| `PORT` | `8080` | |

### Why seeding is guarded on the file not existing

`scripts/seed_demo.py` deletes and rebuilds the database it is pointed at. The existence
guard in `deploy/entrypoint.sh` is the only thing standing between a container restart and
the erasure of every reviewer decision — which is the entire product.

---

## Verified

Checked against the built image, not asserted:

- builds, boots, and serves in ~2s
- `/`, `/exceptions/{id}`, `/approvals`, `/healthz`, `/webhooks/razorpay` all respond
- a decision posted to the approval screen persists, and the queue count drops
- `RESULTS_DIR` resolves to `/app/evals/results` in the container, so the screen's measured
  figures load — this is why the image puts the source on `PYTHONPATH` instead of installing
  the wheel, which would move the module into site-packages and break that relative path
- **the durability loop end to end**: write a decision, destroy the container, start a fresh
  one — Litestream restores the database, the decision is still there, and the seed correctly
  does *not* re-run
