# MoneyPrinterTurbo — 24/7 production worker (deployment-ready)

A **single-instance, always-running video queue + private API**, designed for **Render paid Docker web service + persistent SSD disk**. It uses the pinned upstream [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) CLI and includes the initial ForwardKit Digital and QualifiedLeads-AI promotional scenes and scripts.

**Deployment status:** Code and automated tests are prepared. Render deployment may require manual persistent-disk attachment because the current connector cannot create disks. The worker fails closed until a real 10 GB disk is attached at `/data`. The existing Vercel Sandbox used for earlier test videos is **not** a permanent worker.

## How it works

1. The owner creates a job through an authenticated API. A script and media are required; the included starter presets supply both so no AI script-generation keys are needed.
2. One continuously running worker thread atomically claims the next job from a **SQLite WAL queue on `/data/jobs.sqlite3`**.
3. MoneyPrinterTurbo executes with `--video-source local`, `9:16`, `--voice-name en-US-JennyNeural-Female`, and **no paid video generation services or background music**. It renders under a pinned upstream commit rather than pulling latest code during each job.
4. On success, the finished MP4 is copied and atomically published to **`/data/outputs/<job-id>.mp4`** and its SHA-256 is recorded in SQLite. On failure it retries with an exponential backoff, then parks the job as failed after three attempts. On crash, a lease is reclaimed and retried.
5. Only an authenticated owner can view queue status, upload source footage, cancel jobs, retry failed jobs, or download and delete completed videos.
6. Render keeps its paid service running and restarts failed instances. A persistent disk survives ordinary restarts and deployments; Render also snapshots disks daily. Disk snapshots are **not equivalent** to real-time cross-region replication or guaranteed zero data loss.

No autopublishing to Instagram, TikTok or YouTube is wired in. No paid third-party AI account or API key is required for the starter preset workflow. **Edge TTS relies on external network access and could become unavailable**; use `"voice_name":"no-voice"` as a fallback.

## Estimated hosting charges (October 11, 2026)

Current advertised Render monthly rates: `2c-4g` = **$85/month**, with **10 GB disk × $0.25/GB = $2.50/month**. Estimated baseline **$87.50/month** before taxes, bandwidth and any excess usage. Smaller `1c-2g` is $25/month but has not been load-tested for the video-generation workload, so the deployment file defaults to the 2 CPU / 4 GB class already exercised in the previous cloud sandbox. This is a **paid deployment**. Do not click Create/Apply unless you approve the bill.

## Deploy

**Recommended: separate GitHub repository** named `moneyprinterturbo-production` rather than changing the websites' existing production repositories.

1. Create a new GitHub repository and add the *contents* of this directory at its root. Commit `Dockerfile`, `render.yaml`, `docker-entrypoint.sh`, `workerapp/`, `starter/`, and `requirements-api.txt`. Never commit `.env`, secrets, or MP4 user uploads.
2. In Render, create a **Blueprint** from that GitHub repo or create a single paid Docker web service from its Dockerfile. Render finds `render.yaml` at repo root. Review **2c-4g** and 10 GB disk before approval. `autoDeploy: false` avoids unreviewed future deployments; deploys can be deliberately triggered in Render.
3. The Blueprint automatically generates an `MPT_WORKER_API_TOKEN` secret. Keep it in Render environment variables. Retrieve it only for trusted administrative requests. Do not paste it into public chats, website frontends, public CI logs, or browser JavaScript.
4. On first deploy, visit `https://<your-service>.onrender.com/healthz` to check for `{"ok":true}`. From a private device or backend client, use your API token to call `GET /v1/status` and queue a test job. The health endpoint is intentionally non-sensitive.
5. Confirm one end-to-end video returns `succeeded` and download the MP4. Review video quality and costs before enabling recurring submissions from a trusted server-side scheduler.

**Important:** This is **one process/one Render instance** (`uvicorn ... --workers 1`). Do not manually scale service instances or attach the disk to multiple workers. Render's persistent disks cannot be mounted into multiple instances simultaneously. We use an authenticated web service with an internal worker thread so both API and worker can safely share one disk without adding Postgres and a second instance.

### API examples

Use these from a trusted local terminal. Replace `$BASE_URL` and `$MPT_WORKER_API_TOKEN` with your real Render service URL and private token. Avoid running these in public client JavaScript.

```sh
# Health (public and minimal)
curl "$BASE_URL/healthz"

# Check storage, queue and worker status
curl -H "Authorization: Bearer $MPT_WORKER_API_TOKEN" "$BASE_URL/v1/status"

# Queue one ForwardKit video, using included branded media and script
curl -X POST "$BASE_URL/v1/jobs" \
  -H "Authorization: Bearer $MPT_WORKER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"brand":"forwardkit","idempotency_key":"forwardkit-launch-001"}'

# Queue one QualifiedLeads-AI teaser
curl -X POST "$BASE_URL/v1/jobs" \
  -H "Authorization: Bearer $MPT_WORKER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"brand":"qualifiedleads","idempotency_key":"qualifiedleads-launch-001"}'

# Get status of a specific job (replace JOB_ID)
curl -H "Authorization: Bearer $MPT_WORKER_API_TOKEN" "$BASE_URL/v1/jobs/JOB_ID"

# Download a completed MP4
curl -L -H "Authorization: Bearer $MPT_WORKER_API_TOKEN" \
  "$BASE_URL/v1/jobs/JOB_ID/download" -o marketing-video.mp4
```

For **custom source footage**, create a draft job with `{"brand":"forwardkit","use_starter":false,"auto_queue":false,"script":"...at least 20 characters..."}`, upload 1–12 valid PNG/JPEG/MP4/MOV media items with `POST /v1/jobs/JOB_ID/media` as multipart form field `file`, then `POST /v1/jobs/JOB_ID/enqueue`.

Owner-only maintenance:

- `GET /v1/jobs` — most recent jobs; `GET /v1/status` — totals and worker state
- `POST /v1/jobs/JOB_ID/cancel` — cancel draft, queued or active job
- `POST /v1/jobs/JOB_ID/retry` — reset and requeue a terminal failed job
- `DELETE /v1/jobs/JOB_ID` — permanently delete a completed, failed or canceled job and its stored files

## Durability, security and cost controls

- **Primary state:** SQLite `WAL` with full synchronous write mode; jobs, attempts, and leases are persisted on Render disk.
- **Primary media:** Uploaded files and MP4 outputs stored only on `/data` and have recorded SHA-256 digests; no hotlink/public download endpoint.
- **Disk protection:** The 10 GB disk has an 8 GiB configured limit for permanent assets, leaving additional space for encoding scratch files. When space is low, new jobs and uploads are refused instead of silently losing data.
- **Backups:** Render automatically snapshots paid attached disks daily. Protect irreplaceable deliverables with a second independent offsite backup solution if the business requires tighter recovery guarantees. Download or back up old results before deleting them. No external bucket or backup charges are enabled by this starter.
- **Liveness:** Render health checks `/healthz`, including that the worker thread is alive, so crashed workers lead to a service restart.
- **Retries:** Three total attempts with backoff, a 120s recoverable lease and a 40-minute per-job render timeout. Jobs interrupted mid-render are retried after their lease expires. The output file is published with an atomic rename before success is committed to the queue.
- **Authentication:** 256-bit generated secret; every job, upload and video endpoint requires `Authorization: Bearer ...`. No keys are stored in images or job records. Public service only exposes a minimal health endpoint without authorization.
- **Source security:** The upstream MoneyPrinterTurbo commit is pinned to `4204d0f39afa8ada6f3edc31604141f959fe254e` and Python dependencies use upstream's `uv.lock` with `--frozen`. API packages are pinned. Before later upgrading upstream, review changes and test video rendering.
- **Network:** Edge TTS and upstream setup require outbound internet. Media uploads are restricted by extension, image decoding/size and video probing; this reduces but does not eliminate the risk of processing untrusted media. Only let a trusted operator submit files.
- **No autonomous posting:** All outputs stay private until you explicitly approve distribution. This avoids platform violations, wrong claims, and accidental duplicate publishing.

## Local automated tests

From a Python environment with FastAPI, Pillow, pytest, uvicorn and python-multipart installed:

```sh
PYTHONPATH=. pytest -q tests
```

These tests cover API authentication, queue persistence, leases, retries, uploads, cancellation, idempotency, output publication, and deletion using a mocked MoneyPrinterTurbo process. **The tests do not replace a real Render deployment smoke test or the separate upstream end-to-end video render already completed in Vercel.**

## Follow-on production enhancements

Add periodic independent object-storage backup, error alerts, automated script moderation, content calendar/scheduler, and an approved publishing workflow after the first paid instance is running reliably. Those are intentionally not assumed present in this version.

**GitHub branch deployment:** If using a separate deployment branch in an existing repo, set the Docker context and Dockerfile path to the folder containing this package. Keep production website branches untouched. The deployed service refuses to start until `/data` is an actual mount point, preventing accidental use of ephemeral storage.
