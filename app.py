"""Behna Archive Virtual Lab — observability dashboard + run controls (FastAPI)."""
import os, subprocess, json, io, zipfile
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.extras import RealDictCursor, Json
import core, personas

BASE = os.path.expanduser("~/behna_lab")
app = FastAPI(title="Behna Virtual Lab")


def q(sql, args=(), one=False):
    conn = core.db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, args)
    rows = cur.fetchall(); cur.close(); conn.close()
    return (rows[0] if rows else None) if one else rows


# ---------- Persona defaults ----------
@app.get("/api/personas/defaults")
def persona_defaults():
    """Return the default persona prompts and peer lenses for pre-filling the editor."""
    return {
        "candidate": personas.CANDIDATE,
        "advisor": personas.ADVISOR,
        "peer_template": personas.PEER,
        "peer_lenses": personas.PEER_LENSES,
        "n_chapters": 3,
    }


# ---------- Runs ----------
@app.get("/api/runs")
def api_runs():
    return q("SELECT id, topic, status, phase, created_at, updated_at, control, config "
             "FROM lab.runs ORDER BY id DESC")


@app.get("/api/corpus_stats")
def corpus_stats():
    r = q("SELECT count(*) c, count(embedding) e FROM lab.behna_chunks", one=True)
    return {"documents": r["c"], "embedded": r["e"]}


@app.post("/api/runs")
async def create_run(req: Request):
    body = await req.json()
    topic = (body.get("topic") or "").strip()
    if not topic:
        return JSONResponse({"error": "topic required"}, status_code=400)
    config = {
        "candidate": (body.get("candidate") or personas.CANDIDATE).strip(),
        "advisor": (body.get("advisor") or personas.ADVISOR).strip(),
        "peer_template": (body.get("peer_template") or personas.PEER).strip(),
        "peer_lenses": body.get("peer_lenses") or personas.PEER_LENSES,
        "n_chapters": int(body.get("n_chapters") or 3),
    }
    row = q("INSERT INTO lab.runs (topic, persona, status, config) VALUES (%s,%s,'created',%s) RETURNING id",
            (topic, "custom", Json(config)), one=True)
    rid = row["id"]
    subprocess.Popen([f"{BASE}/venv/bin/python", f"{BASE}/worker.py", str(rid)],
                     cwd=BASE, stdout=open(f"{BASE}/run_{rid}.log", "w"),
                     stderr=subprocess.STDOUT)
    return {"id": rid}


@app.post("/api/runs/{rid}/control")
async def control(rid: int, req: Request):
    body = await req.json()
    action = body.get("action")
    if action not in ("run", "pause", "stop"):
        return JSONResponse({"error": "bad action"}, status_code=400)
    core.set_run(rid, control=action)
    return {"ok": True, "control": action}


@app.get("/api/runs/{rid}/events")
def api_events(rid: int, after: int = 0):
    rows = q("SELECT id, ts, phase, agent, kind, title, content, meta FROM lab.events "
             "WHERE run_id=%s AND id>%s ORDER BY id ASC", (rid, after))
    run = q("SELECT id, topic, status, phase, control, config FROM lab.runs WHERE id=%s", (rid,), one=True)
    return {"run": run, "events": rows}


@app.get("/api/runs/{rid}/personas")
def api_run_personas(rid: int):
    """Return the full persona config used for a specific run."""
    run = q("SELECT id, topic, config FROM lab.runs WHERE id=%s", (rid,), one=True)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return run.get("config") or {}


@app.get("/api/runs/{rid}/artifacts")
def api_artifacts(rid: int):
    return q("SELECT id, kind, title, version, approved, created_at, content "
             "FROM lab.artifacts WHERE run_id=%s ORDER BY id ASC", (rid,))


@app.get("/api/doc/{filename}")
def api_doc(filename: str):
    return q("SELECT filename, doc_type, sender, recipient, creation_date, origin, "
             "languages, summary, transcription, translation FROM lab.behna_chunks "
             "WHERE filename=%s LIMIT 1", (filename,), one=True) or {}


# ---------- Artifact Downloads ----------
def _artifact_filename(a):
    """Generate a clean filename for an artifact."""
    kind = (a.get('kind') or 'artifact').replace(' ', '_')
    title = (a.get('title') or 'untitled').replace(' ', '_').replace('/', '-')[:60]
    return f"{kind}_{title}.md"


@app.get("/api/runs/{rid}/artifacts/{aid}/download")
def download_artifact(rid: int, aid: int):
    a = q("SELECT id, kind, title, version, approved, content FROM lab.artifacts "
          "WHERE run_id=%s AND id=%s", (rid, aid), one=True)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    content = (a['content'] or '').encode('utf-8')
    fname = _artifact_filename(a)
    return StreamingResponse(
        io.BytesIO(content),
        media_type='text/markdown',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'}
    )


@app.get("/api/runs/{rid}/artifacts/download-all")
def download_all_artifacts(rid: int):
    arts = q("SELECT id, kind, title, version, approved, content "
             "FROM lab.artifacts WHERE run_id=%s ORDER BY id ASC", (rid,))
    if not arts:
        return JSONResponse({"error": "no artifacts"}, status_code=404)
    run = q("SELECT topic FROM lab.runs WHERE id=%s", (rid,), one=True)
    topic_slug = (run['topic'] or 'run')[:40].replace(' ', '_').replace('/', '-')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, a in enumerate(arts, 1):
            fname = f"{i:02d}_{_artifact_filename(a)}"
            zf.writestr(fname, a['content'] or '')
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="behna_lab_{topic_slug}.zip"'}
    )


@app.get("/reader/{rid}/{aid}", response_class=HTMLResponse)
def reader_page(rid: int, aid: int):
    return open(f"{BASE}/reader.html", encoding="utf-8").read()


@app.get("/", response_class=HTMLResponse)
def index():
    return open(f"{BASE}/index.html", encoding="utf-8").read()
