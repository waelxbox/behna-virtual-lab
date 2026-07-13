"""Shared core for the Behna Virtual Lab: config, DB access, LLM + embedding
calls, RAG retrieval over lab.behna_chunks, and observability event logging."""
import os, json, time, requests, psycopg2
from psycopg2.extras import RealDictCursor, Json

ENV_PATH = os.path.expanduser("~/behna_lab/.env")

def load_env(path=ENV_PATH):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k] = v
    return env

ENV = load_env()
DB_URL = ENV["SUPABASE_DIRECT_URL"]
GEMINI_KEY = ENV["GEMINI_API_KEY"]
LLM_BASE = ENV.get("OPENAI_BASE_URL", "https://api.manus.im/api/llm-proxy/v1")
LLM_KEY = ENV["OPENAI_API_KEY"]
AGENT_MODEL = ENV.get("AGENT_MODEL", "gemini-3.1-pro-preview")
EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"


def db():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    return conn


# ---------- LLM ----------
def chat(messages, model=None, max_tokens=4000, temperature=0.7, retries=4):
    """Gemini is a reasoning model: reasoning tokens count against max_tokens and can
    starve the visible answer. We retry with a larger budget if content comes back empty."""
    model = model or AGENT_MODEL
    budgets = [max_tokens, max(max_tokens * 2, 8000), 16000]
    last_err = None
    for budget in budgets:
        payload = {"model": model, "messages": messages,
                   "max_tokens": budget, "temperature": temperature}
        for attempt in range(retries):
            try:
                r = requests.post(f"{LLM_BASE}/chat/completions",
                                  headers={"Authorization": f"Bearer {LLM_KEY}",
                                           "Content-Type": "application/json"},
                                  json=payload, timeout=240)
                if r.status_code == 200:
                    msg = r.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if content:
                        return content
                    break  # empty -> bump budget
                if r.status_code in (429, 500, 502, 503):
                    time.sleep(2 ** attempt); continue
                last_err = f"LLM {r.status_code}: {r.text[:300]}"
                raise RuntimeError(last_err)
            except requests.RequestException as e:
                last_err = str(e); time.sleep(2 ** attempt)
    return "[no content returned by model]"


def agent_say(system, user, **kw):
    return chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}], **kw)


# ---------- Embedding + RAG ----------
def embed(text, retries=4):
    payload = {"model": "models/gemini-embedding-001",
               "content": {"parts": [{"text": text[:18000]}]},
               "outputDimensionality": 3072}
    for attempt in range(retries):
        r = requests.post(f"{EMBED_URL}?key={GEMINI_KEY}", json=payload, timeout=60)
        if r.status_code == 200:
            return r.json()["embedding"]["values"]
        if r.status_code in (429, 500, 503):
            time.sleep(2 ** attempt); continue
        raise RuntimeError(f"embed {r.status_code}: {r.text[:200]}")
    raise RuntimeError("embed failed")


def retrieve(query, k=10):
    """Vector search restricted to the isolated lab table."""
    vec = embed(query)
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM lab.match_behna(%s::vector, %s);", (vec, k))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows


def random_sample(k=20):
    """Retrieve a diverse random sample of documents from the Behna corpus."""
    conn = db(); cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""SELECT filename, doc_type, creation_date, sender, recipient, origin,
                          languages, summary, transcription, translation
                   FROM lab.behna_chunks ORDER BY random() LIMIT %s""", (k,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows


def format_sources(rows):
    out = []
    for r in rows:
        cite = r["filename"]
        body = (r.get("translation") or r.get("transcription") or "")[:1200]
        sim = r.get("similarity")
        sim_str = f"sim={sim:.3f}" if sim is not None else ""
        out.append(
            f"[DOC {cite}] type={r.get('doc_type')} date={r.get('creation_date')} "
            f"sender={r.get('sender')} origin={r.get('origin')} {sim_str}\n"
            f"Summary: {r.get('summary')}\nText: {body}"
        )
    return "\n\n".join(out)


# ---------- Observability ----------
def log_event(run_id, agent, kind, title, content="", phase=None, meta=None):
    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT INTO lab.events (run_id, phase, agent, kind, title, content, meta)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, phase, agent, kind, title, content, Json(meta or {})))
    cur.close(); conn.close()


def save_artifact(run_id, kind, title, content, version=1, approved=False):
    conn = db(); cur = conn.cursor()
    cur.execute("""INSERT INTO lab.artifacts (run_id, kind, title, version, content, approved)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (run_id, kind, title, version, content, approved))
    aid = cur.fetchone()[0]; cur.close(); conn.close()
    return aid


def set_run(run_id, **fields):
    if not fields: return
    cols = ", ".join(f'"{k}"=%s' for k in fields)
    conn = db(); cur = conn.cursor()
    cur.execute(f"UPDATE lab.runs SET {cols}, updated_at=now() WHERE id=%s",
                (*fields.values(), run_id))
    cur.close(); conn.close()


def get_control(run_id):
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT control FROM lab.runs WHERE id=%s", (run_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row[0] if row else "stop"
