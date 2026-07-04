"""Create an ISOLATED schema for the Behna Virtual Lab so we never touch the
production tables wired to the user's other (TURATH) app.

Everything lives in a dedicated `lab` schema:
  lab.behna_chunks      -- corpus + 3072-dim gemini-embedding-001 vectors
  lab.runs              -- one row per simulation run
  lab.events            -- observability event stream (agent turns, retrievals, gates)
  lab.artifacts         -- proposals / chapters / final thesis
"""
import os, psycopg2

def load_env(path=os.path.expanduser("~/behna_lab/.env")):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env

env = load_env()
conn = psycopg2.connect(env["SUPABASE_DIRECT_URL"])
conn.autocommit = True
cur = conn.cursor()

cur.execute("CREATE SCHEMA IF NOT EXISTS lab;")

# Corpus chunks with their own embeddings (isolated from public.document_embeddings)
cur.execute("""
CREATE TABLE IF NOT EXISTS lab.behna_chunks (
    id            bigserial PRIMARY KEY,
    filename      text,
    doc_type      text,
    sender        text,
    recipient     text,
    creation_date text,
    origin        text,
    destination   text,
    languages     text,
    entities      text,
    keywords      text,
    summary       text,
    transcription text,
    translation   text,
    embed_text    text,
    embedding     vector(3072)
);
""")

# Runs
cur.execute("""
CREATE TABLE IF NOT EXISTS lab.runs (
    id          bigserial PRIMARY KEY,
    topic       text,
    persona     text,
    status      text DEFAULT 'created',
    phase       text DEFAULT 'phase1_pitch',
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now(),
    control     text DEFAULT 'run'         -- run | pause | stop  (UI -> worker signal)
);
""")

# Observability event stream
cur.execute("""
CREATE TABLE IF NOT EXISTS lab.events (
    id          bigserial PRIMARY KEY,
    run_id      bigint REFERENCES lab.runs(id) ON DELETE CASCADE,
    ts          timestamptz DEFAULT now(),
    phase       text,
    agent       text,        -- candidate | advisor | peer | system
    kind        text,        -- message | retrieval | gate | artifact | status
    title       text,
    content     text,
    meta        jsonb
);
""")
cur.execute("CREATE INDEX IF NOT EXISTS events_run_idx ON lab.events(run_id, id);")

# Artifacts (proposals, chapters, thesis)
cur.execute("""
CREATE TABLE IF NOT EXISTS lab.artifacts (
    id          bigserial PRIMARY KEY,
    run_id      bigint REFERENCES lab.runs(id) ON DELETE CASCADE,
    kind        text,        -- proposal_v1 | chapter | thesis ...
    title       text,
    version     int DEFAULT 1,
    content     text,
    approved    boolean DEFAULT false,
    created_at  timestamptz DEFAULT now()
);
""")

# Vector similarity search restricted to the lab table
cur.execute("""
CREATE OR REPLACE FUNCTION lab.match_behna(query_embedding vector(3072), match_count int DEFAULT 6)
RETURNS TABLE(id bigint, filename text, doc_type text, sender text, recipient text,
              creation_date text, origin text, summary text, transcription text,
              translation text, similarity float)
LANGUAGE sql STABLE AS $$
  SELECT c.id, c.filename, c.doc_type, c.sender, c.recipient, c.creation_date,
         c.origin, c.summary, c.transcription, c.translation,
         1 - (c.embedding <=> query_embedding) AS similarity
  FROM lab.behna_chunks c
  WHERE c.embedding IS NOT NULL
  ORDER BY c.embedding <=> query_embedding
  LIMIT match_count;
$$;
""")

print("Schema 'lab' ready: behna_chunks, runs, events, artifacts, match_behna()")
cur.close(); conn.close()
