from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scrap_ecommerce.scraper import CartupScraper, parse_target

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
WEB_DIST = ROOT / "web" / "dist"

app = FastAPI(title="Cartup Scraper")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()


class StartJobBody(BaseModel):
    url: str
    listing_only: bool = False
    max_products: int | None = Field(default=None, ge=1)


def _job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _append(job: dict[str, Any], event: dict[str, Any]) -> None:
    with job["lock"]:
        job["events"].append(event)
        if event.get("current") is not None:
            job["current"] = event["current"]
        if event.get("total") is not None:
            job["total"] = event["total"]
        if event.get("stage"):
            job["stage"] = event["stage"]
        if event.get("message"):
            job["message"] = event["message"]
        if event.get("rows_written") is not None:
            job["rows_written"] = event["rows_written"]
        if event.get("type") == "done":
            job["status"] = "done"
            job["csv"] = event.get("csv")
            job["filename"] = event.get("filename")
            job["count"] = event.get("count", 0)
        elif event.get("type") == "error":
            job["status"] = "error"
            job["message"] = event.get("message") or "Scrape failed"


def _run_job(job_id: str, url: str, listing_only: bool, max_products: int | None) -> None:
    job = _job(job_id)
    if not _run_lock.acquire(blocking=False):
        _append(job, {"type": "error", "message": "Another scrape is already running"})
        return
    try:
        scraper = CartupScraper(
            out_dir=DATA_DIR,
            listing_only=listing_only,
            max_products=max_products,
            on_progress=lambda payload: _append(job, payload),
        )
        job["csv"] = str(scraper.csv_path)
        job["filename"] = scraper.csv_path.name
        scraper.scrape_url(url)
        with job["lock"]:
            if job["status"] == "running":
                job["status"] = "done"
                job["events"].append(
                    {
                        "type": "done",
                        "count": job.get("count") or job.get("current") or 0,
                        "csv": job.get("csv"),
                        "filename": job.get("filename"),
                    }
                )
    except Exception as exc:
        _append(job, {"type": "error", "message": str(exc)})
    finally:
        try:
            scraper.close()
        except NameError:
            pass
        _run_lock.release()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/api/jobs")
def start_job(body: StartJobBody) -> dict[str, Any]:
    url = body.url.strip()
    try:
        target = parse_target(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A scrape is already running")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "url": target["url"],
        "status": "running",
        "stage": "opening",
        "message": "Queued",
        "current": 0,
        "total": None,
        "count": 0,
        "rows_written": 0,
        "csv": None,
        "filename": None,
        "events": [],
        "lock": threading.Lock(),
        "listing_only": body.listing_only,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, target["url"], body.listing_only, body.max_products),
        daemon=True,
    )
    thread.start()
    return {"id": job_id, "url": target["url"]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    with job["lock"]:
        return {
            "id": job["id"],
            "url": job["url"],
            "status": job["status"],
            "stage": job["stage"],
            "message": job["message"],
            "current": job["current"],
            "total": job["total"],
            "count": job["count"],
            "rows_written": job["rows_written"],
            "filename": job["filename"],
        }


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = _job(job_id)

    async def gen():
        last = 0
        while True:
            with job["lock"]:
                events = job["events"][last:]
                status = job["status"]
            for event in events:
                last += 1
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("type") in {"done", "error"}:
                    return
            if status in {"done", "error"} and last >= len(job["events"]):
                return
            await asyncio.sleep(0.12)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/download")
def download_csv(job_id: str) -> FileResponse:
    job = _job(job_id)
    path = Path(job["csv"] or "")
    if not path.exists():
        raise HTTPException(status_code=404, detail="CSV not ready yet")
    return FileResponse(path, filename=path.name, media_type="text/csv")


if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
