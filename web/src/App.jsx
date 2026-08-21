import { useEffect, useMemo, useRef, useState } from 'react'
import './App.css'

const SAMPLE = 'https://cartup.com/category/sports__outdoors'

function IconBolt() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M13 2 4 14h7l-1 8 10-14h-7l0-6Z" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  )
}

export default function App() {
  const [url, setUrl] = useState(SAMPLE)
  const [listingOnly, setListingOnly] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [job, setJob] = useState(null)
  const sourceRef = useRef(null)

  useEffect(() => {
    return () => sourceRef.current?.close()
  }, [])

  const pct = useMemo(() => {
    if (!job?.total) return job?.status === 'done' ? 100 : job?.current ? 8 : 0
    return Math.min(100, Math.round((job.current / job.total) * 100))
  }, [job])

  async function generate(e) {
    e.preventDefault()
    setError('')
    sourceRef.current?.close()
    setBusy(true)
    setJob({
      status: 'running',
      stage: 'opening',
      message: 'Starting…',
      current: 0,
      total: null,
      rowsWritten: 0,
    })
    try {
      const res = await fetch('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, listing_only: listingOnly }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        const detail = data.detail
        const msg = typeof detail === 'string' ? detail : 'Could not start scrape'
        throw new Error(msg)
      }
      const es = new EventSource(`/api/jobs/${data.id}/events`)
      sourceRef.current = es
      es.onmessage = (msg) => {
        const event = JSON.parse(msg.data)
        setJob((prev) => ({
          ...prev,
          id: data.id,
          status: event.type === 'done' ? 'done' : event.type === 'error' ? 'error' : 'running',
          stage: event.stage || prev?.stage,
          message: event.message || prev?.message,
          current: event.current ?? prev?.current ?? 0,
          total: event.total ?? prev?.total,
          count: event.count ?? prev?.count,
          rowsWritten: event.rows_written ?? prev?.rowsWritten ?? 0,
        }))
        if (event.type === 'done' || event.type === 'error') {
          es.close()
          setBusy(false)
          if (event.type === 'error') setError(event.message || 'Scrape failed')
        }
      }
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) setBusy(false)
      }
    } catch (err) {
      setBusy(false)
      setError(err.message)
    }
  }

  return (
    <main className="shell">
      <header className="hero">
        <h1>Cartup scraper</h1>
        <p>Paste a category, shop, or product URL. Products save straight to the database as they scrape — download this run's rows as CSV anytime, no need to wait for the whole run.</p>
      </header>

      <form className="panel" onSubmit={generate}>
        <label className="field">
          <span>Cartup URL</span>
          <div className="row">
            <input
              type="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://cartup.com/category/…"
              autoComplete="off"
            />
            <button className="generate" type="submit" disabled={busy}>
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                <IconBolt />
                {busy ? 'Running' : 'Generate'}
              </span>
            </button>
          </div>
        </label>
        <label className="toggle">
          <input
            type="checkbox"
            checked={listingOnly}
            onChange={(e) => setListingOnly(e.target.checked)}
            disabled={busy}
          />
          Listing only (faster — skip product-page HTML)
        </label>
        {error ? <p className="error">{error}</p> : null}
      </form>

      {job ? (
        <section className="progress-wrap" aria-live="polite">
          <div className="meta">
            <span>{job.message || job.stage}</span>
            <span>
              {job.current || 0}
              {job.total ? ` / ${job.total}` : ''} · {pct}%
            </span>
          </div>
          <div className="bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${pct}%` }} />
          </div>
          <div className="stats">
            <div className="stat">
              <b>{job.status}</b>
              <span>Job status</span>
            </div>
            <div className="stat">
              <b>{job.current || 0}</b>
              <span>{job.stage === 'listing' ? 'Listings collected' : 'Products completed'}</span>
            </div>
            <div className="stat">
              <b>{job.total ?? '—'}</b>
              <span>Target</span>
            </div>
          </div>
        </section>
      ) : null}

      {job ? (
        <section className="download-wrap">
          {job.rowsWritten > 0 ? (
            <>
              <div className="download-info">
                <b>{job.rowsWritten}</b>
                <span>
                  {job.status === 'done'
                    ? 'rows saved — ready to download'
                    : 'rows saved so far — download anytime'}
                </span>
              </div>
              <a className="download" href={`/api/jobs/${job.id}/download`}>
                Download CSV
              </a>
            </>
          ) : (
            <p className="empty">Saving the first products to the database…</p>
          )}
        </section>
      ) : null}
    </main>
  )
}
