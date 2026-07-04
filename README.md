# Behna Archive Virtual Lab

A multi-agent simulation of the PhD lifecycle over the AUC Behna Archives, built with LangGraph and observable through a live web dashboard.

## Overview

This system simulates the complete doctoral research process — from initial pitch through proposal, archival research, chapter drafting, adversarial colloquium, and final thesis synthesis — using autonomous AI agents with distinct personas grounded in the 1,201-document Behna mercantile archive (Alexandria, early 20th century).

## Architecture

| Component | Technology |
|---|---|
| Orchestration | LangGraph (6-phase state machine with conditional edges) |
| Agent Model | Gemini 3.1 Pro (via LLM proxy) |
| Embeddings | Google `gemini-embedding-001` (3072-dim) |
| Vector Store | Supabase Postgres + pgvector (isolated `lab` schema) |
| Backend | FastAPI + Uvicorn |
| Frontend | Single-page HTML/CSS/JS dashboard |
| Deployment | systemd service on persistent cloud computer |

## Agents

- **Candidate** ("Nadia Halim") — PhD researcher with a theoretical lens on cosmopolitan capitalism and material culture
- **Advisor** ("Prof. Reginald Ashworth") — Demanding gatekeeper with structural veto power (`[APPROVED]`/`[REJECTED]`)
- **Peers** — Adversarial colloquium panel with competing theoretical frameworks (Marxist, Postcolonial, Quantitative)

All personas are fully editable per run from the dashboard UI.

## Features

- Full persona editing (Candidate, Advisor, Peer template + lenses) before each run
- Live timeline with color-coded agent turns and scrollable text bodies
- Advisor gating with deterministic phase transitions
- RAG retrieval over the full Behna corpus with clickable document citations
- Artifact viewer (proposals, bibliography, chapters, thesis)
- Run controls (launch, pause, resume, stop)
- Persistent deployment with auto-restart

## Files

| File | Purpose |
|---|---|
| `core.py` | Config, DB access, LLM calls, embedding, RAG retrieval, event logging |
| `personas.py` | Default system prompts for all agents |
| `graph.py` | LangGraph state machine (6 phases, conditional edges) |
| `worker.py` | Runs a single simulation by run ID |
| `app.py` | FastAPI backend (API + dashboard serving) |
| `index.html` | Frontend dashboard |
| `setup_schema.py` | One-time DB schema creation |
| `embed_corpus.py` | One-time corpus embedding script |
| `behna-lab.service` | systemd unit file |

## Setup

1. Create a Python venv and install dependencies:
   ```bash
   python3 -m venv venv
   pip install langgraph langchain-core fastapi uvicorn[standard] jinja2 psycopg2-binary requests
   ```

2. Create `.env` with:
   ```
   SUPABASE_DIRECT_URL=postgresql://...
   SUPABASE_DB_URL=postgresql://...
   GEMINI_API_KEY=...
   OPENAI_API_KEY=...
   OPENAI_BASE_URL=...
   AGENT_MODEL=gemini-3.1-pro-preview
   ```

3. Run `setup_schema.py` to create the `lab` schema, then `embed_corpus.py` to embed the corpus.

4. Start the server:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8000
   ```

## License

Private — AUC Behna Archives research project.
