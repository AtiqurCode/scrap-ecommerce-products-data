from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from scrap_ecommerce.columns import PREFERRED_COLUMNS
from scrap_ecommerce.scraper import CartupScraper, parse_target

ROOT = Path(__file__).resolve().parents[2]
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
    urls: list[str] = Field(min_length=1)
    listing_only: bool = False
    max_products: int | None = Field(default=None, ge=1)
    refresh: bool = False  # by default, products already saved are skipped, not re-fetched


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
        etype = event.get("type")
        if etype == "category_done":
            job["reports"].append(
                {
                    "url": event.get("url"),
                    "kind": event.get("kind"),
                    "new": event.get("new", 0),
                    "updated": event.get("updated", 0),
                    "skipped": event.get("skipped", 0),
                    "processed": event.get("processed", 0),
                }
            )
        elif etype == "category_error":
            job["reports"].append({"url": event.get("url"), "error": event.get("message")})
        elif etype == "done":
            job["status"] = "done"
            job["count"] = event.get("count", 0)
            # the final "done" event carries the authoritative full report list —
            # replace rather than append so nothing's duplicated with the incremental
            # category_done/category_error entries added above during the run.
            if event.get("reports") is not None:
                job["reports"] = event["reports"]
        elif etype == "error":
            job["status"] = "error"
            job["message"] = event.get("message") or "Scrape failed"


def _run_job(job_id: str, urls: list[str], listing_only: bool, max_products: int | None, refresh: bool) -> None:
    job = _job(job_id)
    if not _run_lock.acquire(blocking=False):
        _append(job, {"type": "error", "message": "Another scrape is already running"})
        return
    scraper: CartupScraper | None = None
    try:
        scraper = CartupScraper(
            listing_only=listing_only,
            max_products=max_products,
            skip_existing=not refresh,
            on_progress=lambda payload: _append(job, payload),
            on_row=lambda row: job["rows"].append(row),
        )
        scraper.scrape_urls(urls)
        with job["lock"]:
            if job["status"] == "running":
                job["status"] = "done"
                job["events"].append(
                    {"type": "done", "count": job.get("count") or job.get("current") or 0}
                )
    except Exception as exc:
        _append(job, {"type": "error", "message": str(exc)})
    finally:
        if scraper is not None:
            scraper.close()
        _run_lock.release()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/api/jobs")
def start_job(body: StartJobBody) -> dict[str, Any]:
    urls = [u.strip() for u in body.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="Need at least one URL")
    targets = []
    for url in urls:
        try:
            targets.append(parse_target(url))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if _run_lock.locked():
        raise HTTPException(status_code=409, detail="A scrape is already running")
    job_id = uuid.uuid4().hex[:12]
    clean_urls = [t["url"] for t in targets]
    job = {
        "id": job_id,
        "urls": clean_urls,
        "status": "running",
        "stage": "opening",
        "message": "Queued",
        "current": 0,
        "total": None,
        "count": 0,
        "rows_written": 0,
        "rows": [],
        "reports": [],
        "events": [],
        "lock": threading.Lock(),
        "listing_only": body.listing_only,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, clean_urls, body.listing_only, body.max_products, body.refresh),
        daemon=True,
    )
    thread.start()
    return {"id": job_id, "urls": clean_urls}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _job(job_id)
    with job["lock"]:
        return {
            "id": job["id"],
            "urls": job["urls"],
            "status": job["status"],
            "stage": job["stage"],
            "message": job["message"],
            "current": job["current"],
            "total": job["total"],
            "count": job["count"],
            "rows_written": job["rows_written"],
            "reports": list(job["reports"]),
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
def download_csv(job_id: str) -> Response:
    """Data lives in MySQL now — this exports the rows this job saved as a CSV on the
    fly, so the UI's download button still works without ever persisting a CSV file."""
    job = _job(job_id)
    with job["lock"]:
        rows = list(job["rows"])
    if not rows:
        raise HTTPException(status_code=404, detail="No rows saved yet")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PREFERRED_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="products_{job_id}.csv"'},
    )


if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")


def main() -> None:
    import os

    import uvicorn

    # Defaults to loopback-only for local dev (matches prior behavior exactly).
    # The Docker image sets HOST=0.0.0.0 — a container's published port can't reach
    # a server bound only to its own 127.0.0.1.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(app, host=host, port=port, log_level="info")
