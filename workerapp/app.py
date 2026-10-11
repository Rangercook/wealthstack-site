"""Authenticated owner API for a one-instance always-on video-rendering service."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .core import NotReady, Settings, Store, StoreError, VideoWorker


class JobCreate(BaseModel):
    brand: Literal["forwardkit", "qualifiedleads"]
    script: str | None = Field(default=None, max_length=6000)
    voice_name: str | None = Field(default=None, max_length=80)
    use_starter: bool = True
    auto_queue: bool = True
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=100)


def public_job(row: dict):
    keep = ("id", "brand", "script", "voice_name", "use_starter", "state", "created_at", "updated_at",
            "attempts", "attempt_limit", "last_error", "result_bytes", "sha256", "cancel_requested")
    return {field: row[field] for field in keep}


def create_app(settings: Settings | None = None, *, launch_worker: bool = True):
    if settings is None:
        settings = Settings.from_env()
    store = Store(settings)
    worker = VideoWorker(store)

    @asynccontextmanager
    async def lifespan(app):
        if launch_worker:
            worker.start()
        try:
            yield
        finally:
            worker.shutdown()

    app = FastAPI(title="MoneyPrinterTurbo Private Video Worker",version="1.0.0",
                  docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store=store
    app.state.worker=worker

    def authorize(authorization: str | None = Header(default=None)):
        expected = "Bearer " + settings.api_token
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(401,"Unauthorized",headers={"WWW-Authenticate":"Bearer"})

    @app.get("/healthz")
    def healthz():
        try:
            with store.db() as conn:
                conn.execute("SELECT 1").fetchone()
            healthy = (not launch_worker) or bool(worker.thread and worker.thread.is_alive())
            if not healthy:
                raise HTTPException(503,"Worker not running")
            return {"ok":True}
        except OSError:
            raise HTTPException(503,"Storage unavailable")

    @app.get("/v1/status",dependencies=[Depends(authorize)])
    def status():
        return store.stats() | {"worker_running": bool(worker.thread and worker.thread.is_alive())}

    @app.post("/v1/jobs",status_code=201,dependencies=[Depends(authorize)])
    def create_job(payload: JobCreate):
        try:
            script = payload.script
            if script is None:
                path = settings.starter_dir / "scripts" / (payload.brand + ".txt")
                script = path.read_text(encoding="utf-8").strip()
            return public_job(store.create(brand=payload.brand,script=script,
                voice_name=payload.voice_name or settings.voice_name,
                use_starter=payload.use_starter,auto_queue=payload.auto_queue,
                idempotency_key=payload.idempotency_key))
        except (StoreError, NotReady, FileNotFoundError) as e:
            raise HTTPException(507 if isinstance(e,NotReady) else 422,str(e)) from e

    @app.get("/v1/jobs",dependencies=[Depends(authorize)])
    def jobs(limit: int = Query(default=50,ge=1,le=100)):
        return [public_job(x) for x in store.list_jobs(limit)]

    def require_job(jid: str):
        row=store.get(jid)
        if row is None:
            raise HTTPException(404,"Unknown job ID")
        return row

    @app.get("/v1/jobs/{jid}",dependencies=[Depends(authorize)])
    def get_job(jid:str):
        return public_job(require_job(jid))

    @app.post("/v1/jobs/{jid}/media",status_code=201,dependencies=[Depends(authorize)])
    async def add_media(jid:str, file: UploadFile=File(...)):
        require_job(jid)
        declared_length = file.size or 0
        if declared_length > settings.max_asset_bytes:
            raise HTTPException(413,"Media exceeds 40 MB limit")
        fd,tmppath=tempfile.mkstemp(prefix="mpt-upload-",suffix=Path(file.filename or "uploaded.bin").suffix)
        tmp=Path(tmppath)
        try:
            length=0
            with os.fdopen(fd,"wb") as f:
                while chunk:=await file.read(1024*1024):
                    length+=len(chunk)
                    if length > settings.max_asset_bytes:
                        raise HTTPException(413,"Media exceeds 40 MB limit")
                    f.write(chunk)
            try:
                return store.add_media(jid,tmp,file.filename or "uploaded.bin")
            except NotReady as e:
                raise HTTPException(507,str(e)) from e
            except StoreError as e:
                raise HTTPException(422,str(e)) from e
        finally:
            await file.close()
            tmp.unlink(missing_ok=True)

    @app.post("/v1/jobs/{jid}/enqueue",dependencies=[Depends(authorize)])
    def enqueue(jid:str):
        require_job(jid)
        try:
            return public_job(store.enqueue(jid))
        except StoreError as e:
            raise HTTPException(422,str(e)) from e

    @app.post("/v1/jobs/{jid}/cancel",dependencies=[Depends(authorize)])
    def cancel(jid:str):
        require_job(jid)
        return public_job(store.cancel(jid))

    @app.post("/v1/jobs/{jid}/retry",dependencies=[Depends(authorize)])
    def retry(jid:str):
        require_job(jid)
        try:
            return public_job(store.retry_failed(jid))
        except StoreError as e:
            raise HTTPException(422,str(e)) from e

    @app.delete("/v1/jobs/{jid}",dependencies=[Depends(authorize)])
    def delete(jid:str):
        require_job(jid)
        try:
            store.delete(jid)
            return {"deleted":True}
        except StoreError as e:
            raise HTTPException(409,str(e)) from e

    @app.get("/v1/jobs/{jid}/download",dependencies=[Depends(authorize)])
    def download(jid:str):
        job=require_job(jid)
        if job["state"] != "succeeded":
            raise HTTPException(409,"Video is not ready")
        path=store.output_path(jid)
        if not path.is_file():
            raise HTTPException(503,"Result metadata exists but file is missing; check persistent disk")
        return FileResponse(path,media_type="video/mp4",filename=f"{job['brand']}-{jid}.mp4",
                            headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})

    return app


# Imported by uvicorn in the service runtime. Test clients use create_app with a
# temporary injected Settings instead, avoiding production token requirements.
