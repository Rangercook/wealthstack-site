"""Durable SQLite queue, safe media ingestion, and MoneyPrinterTurbo CLI orchestration.

Single running instance, 1 API process and 1 worker thread. SQLite WAL is on the
same attached local persistent disk as rendered videos. No remote shell, URLs, or
arbitrary user-specified filesystem paths are accepted by the job API.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

BRANDS = ("forwardkit", "qualifiedleads")
VALID_EXTENSIONS = {".png", ".jpg", ".jpeg", ".mp4", ".mov"}
VOICE_PATTERN = re.compile(r"^(?:no-voice|[a-z]{2}-[A-Z]{2}-[A-Za-z0-9]+Neural-(?:Female|Male))$")
ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    mpt_root: Path
    starter_dir: Path
    api_token: str
    max_asset_bytes: int = 40 * 1024 * 1024
    max_media_files: int = 12
    max_disk_bytes: int = 8 * 1024**3
    min_free_bytes: int = 450 * 1024**2
    max_attempts: int = 3
    job_timeout_seconds: int = 2400
    lease_seconds: int = 120
    poll_seconds: float = 2.0
    voice_name: str = "en-US-JennyNeural-Female"

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("MPT_WORKER_API_TOKEN", "")
        if len(token) < 32:
            raise RuntimeError("MPT_WORKER_API_TOKEN must be a randomly generated secret of at least 32 characters")
        return cls(
            data_dir=Path(os.environ.get("MPT_DATA_DIR", "/data")),
            mpt_root=Path(os.environ.get("MPT_ROOT", "/opt/mpt")),
            starter_dir=Path(os.environ.get("MPT_STARTER_DIR", "/opt/worker/starter")),
            api_token=token,
            max_disk_bytes=int(os.environ.get("MPT_MAX_DISK_BYTES", str(8 * 1024**3))),
            job_timeout_seconds=int(os.environ.get("MPT_JOB_TIMEOUT_SECONDS", "2400")),
            voice_name=os.environ.get("MPT_DEFAULT_VOICE", "en-US-JennyNeural-Female"),
        )


class StoreError(ValueError):
    pass


class NotReady(StoreError):
    pass


def now() -> int:
    return int(time.time())


def valid_id(job_id: str) -> bool:
    return bool(ID_PATTERN.fullmatch(job_id))


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


class Store:
    def __init__(self, settings: Settings):
        self.s = settings
        self.s.data_dir.mkdir(parents=True, exist_ok=True)
        for directory in ("jobs", "outputs", "logs", "scratch"):
            (self.s.data_dir / directory).mkdir(exist_ok=True)
        self.dbpath = self.s.data_dir / "jobs.sqlite3"
        self._create_schema()

    def db(self):
        conn = sqlite3.connect(self.dbpath, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _create_schema(self):
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,
                  brand TEXT NOT NULL CHECK (brand IN ('forwardkit','qualifiedleads')),
                  script TEXT NOT NULL,
                  voice_name TEXT NOT NULL,
                  use_starter INTEGER NOT NULL,
                  state TEXT NOT NULL CHECK (state IN ('draft','queued','running','succeeded','failed','canceled')),
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL,
                  next_run_at INTEGER NOT NULL DEFAULT 0,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  attempt_limit INTEGER NOT NULL DEFAULT 3,
                  run_token TEXT,
                  lease_until INTEGER,
                  task_id TEXT,
                  last_error TEXT,
                  result_bytes INTEGER,
                  sha256 TEXT,
                  cancel_requested INTEGER NOT NULL DEFAULT 0,
                  idempotency_key TEXT UNIQUE,
                  payload_hash TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_work_idx ON jobs(state,next_run_at,created_at);
                CREATE TABLE IF NOT EXISTS media (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL REFERENCES jobs(id),
                  saved_path TEXT NOT NULL,
                  original_name TEXT NOT NULL,
                  bytes INTEGER NOT NULL,
                  sha256 TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_job_idx ON media(job_id,created_at);
            """)

    def _row(self, row):
        return dict(row) if row is not None else None

    def get(self, job_id):
        if not valid_id(job_id):
            return None
        with self.db() as db:
            return self._row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit=50):
        with self.db() as db:
            return [self._row(r) for r in db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (min(max(int(limit), 1), 100),)
            ).fetchall()]

    def stats(self):
        with self.db() as db:
            counts = {r["state"]: r["count"] for r in db.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            )}
        return {"counts": counts, "data_bytes": self.data_bytes()}

    def data_bytes(self):
        size = 0
        for folder in ("jobs", "outputs", "logs"):
            for path in (self.s.data_dir / folder).rglob("*"):
                if path.is_file() and not path.is_symlink():
                    size += path.stat().st_size
        return size

    def ensure_capacity(self, added_bytes=0):
        if self.data_bytes() + added_bytes > self.s.max_disk_bytes:
            raise NotReady("Storage quota reached. Archive or remove old videos before queuing more jobs.")
        free = shutil.disk_usage(self.s.data_dir).free
        if free < self.s.min_free_bytes + added_bytes:
            raise NotReady("Insufficient free disk space for rendering; storage was not modified.")

    def create(self, *, brand, script, voice_name, use_starter, auto_queue, idempotency_key):
        if brand not in BRANDS:
            raise StoreError("Invalid brand")
        if not isinstance(script, str) or not 20 <= len(script.strip()) <= 6000:
            raise StoreError("Script must be between 20 and 6000 characters")
        if not VOICE_PATTERN.fullmatch(voice_name):
            raise StoreError("Invalid or unsupported voice. Use Edge Neural format or no-voice")
        if idempotency_key is not None and (not 8 <= len(idempotency_key) <= 100):
            raise StoreError("Idempotency key length must be 8-100 characters")
        if auto_queue and not use_starter:
            raise StoreError("Queueing without starter media requires uploads first")
        self.ensure_capacity()
        t = now()
        jid = str(uuid.uuid4())
        payload_hash = hashlib.sha256(json.dumps(
            [brand, script, voice_name, bool(use_starter)], separators=(",", ":")
        ).encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        raise StoreError("Idempotency key was already used for a different job")
                    return self._row(existing)
            db.execute("""INSERT INTO jobs(id,brand,script,voice_name,use_starter,state,created_at,updated_at,
                 next_run_at,attempt_limit,idempotency_key,payload_hash)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (jid, brand, script, voice_name, int(use_starter), 'queued' if auto_queue else 'draft',
                  t, t, t, self.s.max_attempts, idempotency_key, payload_hash))
        return self.get(jid)

    def media_for(self, jid):
        with self.db() as db:
            return [self._row(r) for r in db.execute(
                "SELECT * FROM media WHERE job_id=? ORDER BY created_at, id", (jid,)
            )]

    def add_media(self, jid, tmp_path: Path, original_name: str):
        record = self.get(jid)
        if not record:
            raise StoreError("Job not found")
        if record["state"] != "draft":
            raise StoreError("Media can be added only to a draft job")
        suffix = Path(original_name).suffix.lower()
        if suffix not in VALID_EXTENSIONS:
            raise StoreError("Only PNG, JPEG, MP4, and MOV media are supported")
        if not tmp_path.is_file() or not 1 <= tmp_path.stat().st_size <= self.s.max_asset_bytes:
            raise StoreError("Media must be 1 byte to 40 MB")
        self.ensure_capacity(tmp_path.stat().st_size)
        if suffix in (".png", ".jpg", ".jpeg"):
            try:
                with Image.open(tmp_path) as im:
                    if im.width < 120 or im.height < 120 or im.width * im.height > 45_000_000:
                        raise StoreError("Image dimensions must be at least 120px and at most 45MP")
                    im.verify()
            except (UnidentifiedImageError, OSError) as e:
                raise StoreError("Media is not a valid image") from e
        else:
            cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", str(tmp_path)]
            try:
                verified = subprocess.run(cmd, capture_output=True, text=True, timeout=12, check=False)
            except (subprocess.TimeoutExpired, OSError) as e:
                raise StoreError("Media validation failed") from e
            if verified.returncode != 0 or "video" not in verified.stdout:
                raise StoreError("Uploaded media is not a supported video")
        media_id = str(uuid.uuid4())
        dest = self.s.data_dir / "jobs" / jid / "assets" / (media_id + suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        copied = False
        try:
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()
                if not current or current["state"] != "draft":
                    raise StoreError("Job is no longer in draft state")
                count = db.execute("SELECT COUNT(*) FROM media WHERE job_id=?", (jid,)).fetchone()[0]
                if count >= self.s.max_media_files:
                    raise StoreError("Maximum media files for this job reached")
                shutil.copy2(tmp_path, dest)
                copied = True
                db.execute("INSERT INTO media (id,job_id,saved_path,original_name,bytes,sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                           (media_id, jid, str(dest), Path(original_name).name[:128], dest.stat().st_size,
                            file_hash(dest), now()))
        except Exception:
            if copied:
                dest.unlink(missing_ok=True)
            raise
        return {"id": media_id, "bytes": dest.stat().st_size, "sha256": file_hash(dest)}

    def enqueue(self, jid):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not record:
                raise StoreError("Job not found")
            if record["state"] != "draft":
                raise StoreError("Only draft jobs can be queued")
            if not record["use_starter"] and not db.execute(
                "SELECT 1 FROM media WHERE job_id=? LIMIT 1", (jid,)).fetchone():
                raise StoreError("Upload at least one video or image before queueing")
            db.execute("UPDATE jobs SET state='queued',updated_at=?,next_run_at=? WHERE id=?", (now(), now(), jid))
        return self.get(jid)

    def cancel(self, jid):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not record:
                raise StoreError("Job not found")
            if record["state"] in ("draft", "queued"):
                db.execute("UPDATE jobs SET state='canceled',cancel_requested=1,updated_at=? WHERE id=?",
                           (now(), jid))
            elif record["state"] == "running":
                db.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (now(), jid))
        return self.get(jid)

    def delete(self, jid):
        """Owner-only removal to reclaim storage; never delete an active job."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT state FROM jobs WHERE id=?",(jid,)).fetchone()
            if not row:
                raise StoreError("Job not found")
            if row["state"] in ("running","queued"):
                raise StoreError("Cancel queued jobs before deletion and wait for active renders to stop")
            db.execute("DELETE FROM media WHERE job_id=?",(jid,))
            db.execute("DELETE FROM jobs WHERE id=?",(jid,))
        self.output_path(jid).unlink(missing_ok=True)
        (self.s.data_dir/"logs"/(jid+".log")).unlink(missing_ok=True)
        shutil.rmtree(self.s.data_dir/"jobs"/jid,ignore_errors=True)
        return True

    def retry_failed(self, jid):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT state FROM jobs WHERE id=?",(jid,)).fetchone()
            if not row:
                raise StoreError("Job not found")
            if row["state"] != "failed":
                raise StoreError("Only failed jobs can be retried")
            db.execute("""UPDATE jobs SET state='queued',attempts=0,run_token=NULL,lease_until=NULL,
                          cancel_requested=0,updated_at=?,next_run_at=?,last_error=NULL WHERE id=?""",
                       (now(),now(),jid))
        return self.get(jid)

    def requeue_stale(self):
        t = now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE jobs SET state='queued',run_token=NULL,lease_until=NULL,updated_at=?,
                next_run_at=?,last_error='Recovered after worker restart'
                WHERE state='running' AND lease_until < ? AND attempts < attempt_limit""", (t,t,t))
            db.execute("""UPDATE jobs SET state='failed',run_token=NULL,lease_until=NULL,updated_at=?,
                last_error='Worker interrupted and retry limit reached'
                WHERE state='running' AND lease_until < ? AND attempts >= attempt_limit""", (t,t))

    def claim(self):
        t = now()
        self.requeue_stale()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT id FROM jobs WHERE state='queued' AND next_run_at<=?
                                 ORDER BY created_at,id LIMIT 1""", (t,)).fetchone()
            if not row:
                return None
            jid = row["id"]
            token = str(uuid.uuid4())
            task_id = str(uuid.uuid4())
            db.execute("""UPDATE jobs SET state='running',run_token=?,task_id=?,attempts=attempts+1,
                         lease_until=?,updated_at=?,last_error=NULL WHERE id=?""",
                       (token, task_id, t+self.s.lease_seconds, t,jid))
        return self.get(jid)

    def heartbeat(self, jid, token):
        with self.db() as db:
            db.execute("""UPDATE jobs SET lease_until=?,updated_at=? WHERE id=?
                AND state='running' AND run_token=?""",
                (now()+self.s.lease_seconds, now(),jid,token))
            return db.total_changes > 0

    def success(self, jid, token, output: Path):
        expected = self.s.data_dir / "outputs" / (jid + ".mp4")
        if output != expected or not output.is_file() or output.stat().st_size == 0:
            raise StoreError("Rendered output missing or outside output directory")
        t = now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,run_token,cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row or row["state"] != "running" or row["run_token"] != token:
                raise StoreError("Job lease expired before publishing output")
            if row["cancel_requested"]:
                raise StoreError("Job was canceled before publishing output")
            db.execute("""UPDATE jobs SET state='succeeded',run_token=NULL,lease_until=NULL,
                result_bytes=?,sha256=?,updated_at=? WHERE id=?""",
                (output.stat().st_size, file_hash(output), t, jid))
        return self.get(jid)

    def fail(self, jid, token, error: str, *, canceled=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row or row["state"] != "running" or row["run_token"] != token:
                return
            do_cancel = canceled or bool(row["cancel_requested"])
            terminal = do_cancel or row["attempts"] >= row["attempt_limit"]
            new_state = "canceled" if do_cancel else "failed" if terminal else "queued"
            backoff = min(900, 30 * 2 ** max(0, row["attempts"]-1))
            db.execute("""UPDATE jobs SET state=?,updated_at=?,run_token=NULL,lease_until=NULL,
                next_run_at=?,last_error=? WHERE id=?""",
                (new_state, now(), now() + (0 if terminal else backoff), error[:500], jid))

    def media_paths(self, job):
        if job["use_starter"]:
            paths = sorted((self.s.starter_dir / job["brand"]).glob("scene-*.png"))
        else:
            paths = [Path(record["saved_path"]) for record in self.media_for(job["id"])]
        if not paths or any(not p.is_file() for p in paths):
            raise StoreError("Required media files are missing")
        return paths

    def output_path(self, jid):
        if not valid_id(jid):
            raise StoreError("Invalid job identifier")
        return self.s.data_dir / "outputs" / (jid + ".mp4")


class VideoWorker:
    def __init__(self, store: Store):
        self.store = store
        self.stop = threading.Event()
        self.thread = None
        self.active_process = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.loop, name="video-render-worker", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=20)

    def loop(self):
        while not self.stop.is_set():
            try:
                job = self.store.claim()
                if job:
                    self.render(job)
                    continue
            except Exception as e:
                print(f"Worker loop issue: {type(e).__name__}: {e}", flush=True)
            self.stop.wait(self.store.s.poll_seconds)

    def render(self, job):
        s = self.store.s
        jid, task_id, token = job["id"], job["task_id"], job["run_token"]
        proc = None
        log_path = s.data_dir / "logs" / (jid + ".log")
        cancel_reason = None
        try:
            self.store.ensure_capacity()
            paths = self.store.media_paths(job)
            cli = s.mpt_root / "cli.py"
            python = s.mpt_root / ".venv" / "bin" / "python"
            if not cli.is_file() or not python.is_file():
                raise RuntimeError("MoneyPrinterTurbo installation is missing")
            args = [str(python), str(cli), "--task-id", task_id,
                    "--video-script", job["script"], "--video-source", "local",
                    "--video-materials", ",".join(map(str, paths)),
                    "--video-aspect", "9:16", "--video-concat-mode", "sequential",
                    "--video-clip-duration", "4", "--n-threads", "1", "--bgm-type", "none",
                    "--no-subtitle-enabled", "--voice-name", job["voice_name"]]
            env = os.environ.copy()
            env.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "PYTHONUNBUFFERED": "1"})
            started = time.monotonic()
            with log_path.open("ab") as log:
                log.write(("\n--- Attempt " + str(job["attempts"]) + " at " + str(now()) + " ---\n").encode())
                log.flush()
                proc = subprocess.Popen(args, cwd=s.mpt_root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                self.active_process = proc
                while proc.poll() is None:
                    if self.stop.is_set():
                        cancel_reason = "Worker instance shutting down"
                        break
                    current = self.store.get(jid)
                    if current and current["cancel_requested"]:
                        cancel_reason = "User requested cancellation"
                        break
                    if time.monotonic() - started > s.job_timeout_seconds:
                        cancel_reason = "Render exceeded configured time limit"
                        break
                    if not self.store.heartbeat(jid,token):
                        cancel_reason = "Job lease was lost"
                        break
                    self.stop.wait(3)
                if cancel_reason:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=8)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try: os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                    finally:
                        proc.wait()
                else:
                    proc.wait()
            if cancel_reason:
                self.store.fail(jid,token,cancel_reason,canceled="cancellation" in cancel_reason)
                return
            if proc.returncode:
                raise RuntimeError(f"MoneyPrinterTurbo exited {proc.returncode}; check the per-job log")
            produced = s.mpt_root / "storage" / "tasks" / task_id / "final-1.mp4"
            if not produced.is_file() or produced.stat().st_size < 100:
                raise RuntimeError("MoneyPrinterTurbo reported success without final-1.mp4")
            target = self.store.output_path(jid)
            scratch_output = target.with_suffix(".partial")
            shutil.copy2(produced,scratch_output)
            os.replace(scratch_output,target)
            self.store.success(jid,token,target)
            print(f"Video generated: {jid}, bytes={target.stat().st_size}",flush=True)
        except Exception as e:
            self.store.fail(jid,token, f"{type(e).__name__}: {e}")
            print(f"Video render attempt failed: {jid}: {e}",flush=True)
        finally:
            self.active_process = None
            # Keep finished videos and inputs; clean only derived disposable upstream files.
            working = s.mpt_root / "storage" / "tasks" / task_id
            if working.is_dir():
                shutil.rmtree(working,ignore_errors=True)
            # The CLI caches derived local footage under storage/local_videos.
            local = s.mpt_root / "storage" / "local_videos"
            if local.is_dir():
                for child in local.iterdir():
                    if child.is_file():
                        child.unlink(missing_ok=True)
            if log_path.is_file() and log_path.stat().st_size > 256 * 1024:
                with log_path.open("rb") as f:
                    f.seek(-256 * 1024,os.SEEK_END)
                    tail=f.read()
                log_path.write_bytes(tail)
