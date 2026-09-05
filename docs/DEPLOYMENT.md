# Deployment

Two halves, deployed independently.

| | What it is | Where it runs | What it costs |
|---|---|---|---|
| **Showcase** — `site/` | One static page, generated from `evals/results/` | Cloudflare Pages | free |
| **The app** — `src/precedent/api/` | FastAPI, SQLite, both gates, Razorpay webhooks | Google Cloud Run | ~$10–15/mo |

The app is six screens behind one shell:

| Route | |
|---|---|
| `/` | The queue — exceptions the agent would not settle alone |
| `/exceptions/{id}` | The case: tie-out, cited precedents, proposal, **both gates** |
| `/corpus` | Every precedent, split by whether a human wrote it or the system did |
| `/refunds` | The remediation ceiling and every refund proposed against it |
| `/learns` | Why a corpus beats a better prompt, on the classes where it must |
| `/result` | The measurement, read from `evals/results/` at request time |

`/remediation` stays the JSON API. The screen is at `/refunds` because two routes on one path
means the one registered first silently wins.

The split is not arbitrary. The showcase must load with no server, no database, and no cold
start, and every figure on it is read at build time from the JSON the eval harness wrote — so
it stays true whether or not a backend is up. The approval screen is the opposite: it is the
running system, it writes, and what it writes is the corpus.

---

## The gate calls a model, in the request path

Confirming a resolution **authors a precedent** — that is the loop the whole project exists
to close, and `usecases/deposit.py::record_and_deposit` owns one transaction spanning the
review and the deposit. So the approval gate makes an LLM call while the reviewer waits, and
the service needs `NVIDIA_API_KEY` (or `GROQ_API_KEY`) to function.

Without a key the gate **refuses every confirmation** rather than banking a review with
nothing in the corpus behind it. That is deliberate: the two states the design forbids are a
precedent whose resolution was never reviewed, and a review with no precedent behind it. A
loud refusal is cheaper than a silent inconsistency in the corpus. Reject still works — it
deposits nothing by definition.

The same atomicity means a model outage loses the decision too, and the screen says so and
asks for a retry. The Cloud Run timeout is 120s to leave room for the call.

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

## Backend — Render (the no-billing-account path)

Cloud Run below is the better home for this service. It needs a Google Cloud **billing
account**, which an AI-Studio-created project does not have — `gcloud services enable` fails
with `UREQ_PROJECT_BILLING_NOT_FOUND` until one exists. Render's free tier needs no card.

1. [dashboard.render.com](https://dashboard.render.com) → **New → Blueprint**
2. Connect `krishivsaini/Precedent`; Render reads [`render.yaml`](../render.yaml)
3. It prompts once for the four secrets (`sync: false` keeps them out of the repo):
   `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `NVIDIA_API_KEY`
4. Apply. First build takes a few minutes; afterwards every push to `main` redeploys.

### What the free tier costs

| | |
|---|---|
| Sleeps after ~15 min idle | ~50s cold start on the next hit. Razorpay retries on non-2xx, so a webhook landing on a cold instance still arrives. |
| No persistent disk | `/data` is container-local. `PRECEDENT_SEED_ON_EMPTY=1` reseeds the demo corpus on boot, so the app is always populated — but **the corpus does not accumulate across restarts**, which is the one claim this project makes. |

To make it accumulate, set `LITESTREAM_REPLICA_URL` to any S3-compatible bucket. The
entrypoint replicates instead of reseeding, with no image change.

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
Razorpay secrets and the model key to Secret Manager, builds the image with Cloud Build, and
deploys.

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

## Keeping a public demo from being spent

`PRECEDENT_WRITE_KEY` gates every state-changing request. Reads are untouched — the queue, a
case, the corpus, the measurement and the delivery log stay open, because the argument is the
product. Only writes need the key, because on a public URL a single POST spends model credits,
writes into a corpus that later cases are resolved from, and can reach a refund gate.

Set it, then open `https://<service-url>/unlock?key=<the key>` once in the browser you demo
from. The key is exchanged for an HttpOnly cookie and the URL redirects to `/`, so it does not
sit in the address bar waiting to appear in a screenshot.

It is **not authentication** — `product_design.md` §6 scopes that out and this does not put it
back. There are no accounts and no identity; everyone holding the key is the same anonymous
operator. Unset means off, so a clone and the test suite never meet it.

The Razorpay webhook is exempt: it verifies an HMAC over the raw body, which is a stronger
check than this one and the only one Razorpay can satisfy.

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
| `NVIDIA_API_KEY` / `GROQ_API_KEY` | *(unset)* | required for Confirm and Correct; see above |
| `PRECEDENT_WRITE_KEY` | *(unset)* | gates state-changing requests; unset means off |

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
- **the learning loop end to end, against a live model**: confirming a case authored a real
  precedent (`prec_0001`, corpus_version 1, `derived_from_resolution=res_0001`), which then
  appeared under "Authored by operation" on `/corpus` and was cited back on the case screen
- **the ceiling actually withholds the button**: a ₹328 duplicate refund exceeds the ₹250
  per-call cap, so `/exceptions/{id}` renders the reason and offers no Approve control at all
- with no model key configured, a confirmation is refused and `human_action` stays `NULL`
