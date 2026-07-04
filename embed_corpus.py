"""Load the full Behna CSV into lab.behna_chunks and embed each row with
gemini-embedding-001 (3072-dim). Idempotent: skips rows already loaded by filename.
Resumable: embeds in batches and commits as it goes."""
import os, csv, time, json, sys
import psycopg2, requests

CSV_PATH = os.path.expanduser("~/behna_lab/data/behna_corpus.csv")
EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"

def load_env(path=os.path.expanduser("~/behna_lab/.env")):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k] = v
    return env

env = load_env()
GEMINI_KEY = env["GEMINI_API_KEY"]

def clean(v):
    if v is None: return None
    v = str(v).strip()
    return None if v in ("", "nan", "null", "None") else v

def build_embed_text(row):
    """Compose a retrieval-friendly block mirroring the production embedding style."""
    parts = []
    for label, col in [("Document", "filename"), ("Type", "document_type"),
                       ("Date", "creation_date"), ("Sender", "sender"),
                       ("Recipient", "recipient"), ("Origin", "origin_location"),
                       ("Destination", "destination_location"),
                       ("Languages", "languages_present"),
                       ("Entities", "mentioned_entities"),
                       ("Keywords", "keywords_items"),
                       ("Summary", "summary")]:
        val = clean(row.get(col))
        if val: parts.append(f"{label}: {val}")
    # Prefer English translation for semantic retrieval, fall back to transcription
    trans = clean(row.get("english_translation")) or clean(row.get("transcription"))
    if trans: parts.append(f"Content: {trans}")
    return "\n".join(parts)

def embed(text, retries=5):
    payload = {"model": "models/gemini-embedding-001",
               "content": {"parts": [{"text": text[:18000]}]},
               "outputDimensionality": 3072}
    for attempt in range(retries):
        try:
            r = requests.post(f"{EMBED_URL}?key={GEMINI_KEY}", json=payload, timeout=60)
            if r.status_code == 200:
                return r.json()["embedding"]["values"]
            if r.status_code in (429, 500, 503):
                time.sleep(2 ** attempt); continue
            raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
        except requests.RequestException:
            time.sleep(2 ** attempt)
    raise RuntimeError("embedding failed after retries")

conn = psycopg2.connect(env["SUPABASE_DIRECT_URL"])
conn.autocommit = False
cur = conn.cursor()

# Which filenames already embedded (resume support)
cur.execute("SELECT filename FROM lab.behna_chunks WHERE embedding IS NOT NULL;")
done = {r[0] for r in cur.fetchall()}
print(f"Already embedded: {len(done)}")

with open(CSV_PATH, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
print(f"CSV rows: {len(rows)}")

inserted = 0
for i, row in enumerate(rows):
    fn = clean(row.get("filename")) or f"row_{i}"
    if fn in done:
        continue
    etext = build_embed_text(row)
    if not etext.strip():
        continue
    try:
        vec = embed(etext)
    except Exception as e:
        print(f"[{i}] FAILED {fn}: {e}"); continue
    cur.execute("""
        INSERT INTO lab.behna_chunks
        (filename, doc_type, sender, recipient, creation_date, origin, destination,
         languages, entities, keywords, summary, transcription, translation, embed_text, embedding)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        fn, clean(row.get("document_type")), clean(row.get("sender")),
        clean(row.get("recipient")), clean(row.get("creation_date")),
        clean(row.get("origin_location")), clean(row.get("destination_location")),
        clean(row.get("languages_present")), clean(row.get("mentioned_entities")),
        clean(row.get("keywords_items")), clean(row.get("summary")),
        clean(row.get("transcription")), clean(row.get("english_translation")),
        etext, vec,
    ))
    inserted += 1
    if inserted % 25 == 0:
        conn.commit()
        print(f"  committed {inserted} (row {i+1}/{len(rows)})", flush=True)

conn.commit()
cur.execute("SELECT count(*) FROM lab.behna_chunks WHERE embedding IS NOT NULL;")
total = cur.fetchone()[0]
print(f"DONE. Inserted this run: {inserted}. Total embedded: {total}")
cur.close(); conn.close()
