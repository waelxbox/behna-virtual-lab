"""The Behna Virtual Lab: a LangGraph state machine simulating the PhD lifecycle.

Phases:
  1 pitch        Candidate pitches a topic; Advisor accepts/refines.
  2 proposal     Iterative proposal loop, gated by Advisor [APPROVED].
  3 archive      Candidate runs RAG queries, builds annotated bibliography of primary sources.
  4 drafting     Chapter-by-chapter; each chapter gated by Advisor [APPROVED].
  5 colloquium   One chapter defended before adversarial Peer panel; revised.
  6 thesis       Final synthesis of approved chapters.

Deterministic guardrails use LangGraph conditional edges keyed on the Advisor's
explicit VERDICT flag. The worker also honors UI control signals (pause/stop).
"""
import time, re
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END

import core, personas

MAX_PROPOSAL_ITERS = 3
N_CHAPTERS = 3            # kept modest for a tractable run; raise for full dissertation
MAX_CHAPTER_REVISIONS = 2


class LabState(TypedDict, total=False):
    run_id: int
    topic: str
    proposal: str
    proposal_iter: int
    bibliography: str
    chapters: List[Dict[str, Any]]
    current_chapter: int
    colloquium_done: bool
    thesis: str


def _approved(text: str) -> bool:
    return bool(re.search(r"VERDICT:\s*\[APPROVED\]", text or "", re.I))


def _control_gate(run_id):
    """Block while paused; raise to stop."""
    while True:
        c = core.get_control(run_id)
        if c == "stop":
            raise RuntimeError("STOPPED_BY_USER")
        if c != "pause":
            return
        core.set_run(run_id, status="paused")
        time.sleep(3)


# ---------------- Nodes ----------------
def node_pitch(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase1_pitch", status="running")
    core.log_event(rid, "system", "status", "Phase 1 — Application & Conceptualization", phase="phase1_pitch")

    pitch = core.agent_say(personas.CANDIDATE,
        f"Pitch a focused doctoral research question on the Behna Archives related to: '{state['topic']}'. "
        "2-3 paragraphs: the question, why it matters, and the angle your theoretical lens brings.")
    core.log_event(rid, "candidate", "message", "Research pitch", pitch, phase="phase1_pitch")

    review = core.agent_say(personas.ADVISOR,
        f"The candidate pitched:\n\n{pitch}\n\nEvaluate the potential of this doctoral topic and either accept "
        "the student (refining the question) or send them back. Remember your VERDICT line.")
    core.log_event(rid, "advisor", "gate", "Advisor on pitch", review, phase="phase1_pitch",
                   meta={"approved": _approved(review)})
    state["topic"] = state["topic"]
    return state


def node_proposal(state: LabState) -> LabState:
    rid = state["run_id"]
    it = state.get("proposal_iter", 0) + 1
    state["proposal_iter"] = it
    core.set_run(rid, phase="phase2_proposal")
    core.log_event(rid, "system", "status", f"Phase 2 — Proposal (iteration {it})", phase="phase2_proposal")

    _control_gate(rid)
    prior = state.get("proposal", "")
    prompt = (f"Write Version {it} of your research proposal on '{state['topic']}'. Include: research question, "
              "methodology, theoretical framework, and a short annotated bibliography of SECONDARY literature. "
              "~600 words.")
    if prior:
        prompt += f"\n\nYour previous version and the advisor's critique are below; revise accordingly.\n\nPREVIOUS:\n{prior}"
    proposal = core.agent_say(personas.CANDIDATE, prompt, max_tokens=3000)
    state["proposal"] = proposal
    core.log_event(rid, "candidate", "message", f"Proposal v{it}", proposal, phase="phase2_proposal")
    core.save_artifact(rid, f"proposal_v{it}", f"Proposal v{it}", proposal, version=it)

    review = core.agent_say(personas.ADVISOR,
        f"Review proposal Version {it}:\n\n{proposal}\n\nApply doctoral standards. Remember your VERDICT line.")
    ok = _approved(review)
    core.log_event(rid, "advisor", "gate", f"Advisor on proposal v{it}", review,
                   phase="phase2_proposal", meta={"approved": ok})
    state["_proposal_ok"] = ok
    return state


def route_proposal(state: LabState) -> str:
    if state.get("_proposal_ok"):
        return "archive"
    if state.get("proposal_iter", 0) >= MAX_PROPOSAL_ITERS:
        return "archive"   # advisor's last critique stands; proceed to keep sim moving
    return "proposal"


def node_archive(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase3_archive")
    core.log_event(rid, "system", "status", "Phase 3 — Archival Deep-Dive (RAG)", phase="phase3_archive")

    # Candidate formulates search queries
    qtext = core.agent_say(personas.CANDIDATE,
        f"Based on your approved proposal on '{state['topic']}', list exactly 3 concrete search queries "
        "you would run against the Behna archive to find primary evidence. One per line, no numbering.")
    queries = [q.strip("-• ").strip() for q in qtext.splitlines() if q.strip()][:3]
    core.log_event(rid, "candidate", "message", "Archive search queries", "\n".join(queries), phase="phase3_archive")

    all_sources = []
    for q in queries:
        rows = core.retrieve(q, k=5)
        all_sources.extend(rows)
        core.log_event(rid, "system", "retrieval", f"Retrieved for: {q}",
                       core.format_sources(rows), phase="phase3_archive",
                       meta={"query": q, "doc_ids": [r["filename"] for r in rows]})

    src_block = core.format_sources(all_sources[:12])
    biblio = core.agent_say(personas.CANDIDATE,
        f"Here are primary documents retrieved from the Behna archive:\n\n{src_block}\n\n"
        "Write an annotated bibliography of the most relevant PRIMARY sources (cite each by its Doc filename ID), "
        "noting what each contributes to your argument.", max_tokens=3000)
    state["bibliography"] = biblio
    core.log_event(rid, "candidate", "message", "Annotated primary-source bibliography", biblio, phase="phase3_archive")
    core.save_artifact(rid, "bibliography", "Annotated Primary-Source Bibliography", biblio)
    state["chapters"] = []
    state["current_chapter"] = 0
    return state


def node_chapter(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    n = state.get("current_chapter", 0) + 1
    state["current_chapter"] = n
    core.set_run(rid, phase="phase4_drafting")
    core.log_event(rid, "system", "status", f"Phase 4 — Drafting Chapter {n}", phase="phase4_drafting")

    # fresh retrieval to ground the chapter
    rows = core.retrieve(f"{state['topic']} chapter {n} evidence", k=6)
    core.log_event(rid, "system", "retrieval", f"Chapter {n} evidence", core.format_sources(rows),
                   phase="phase4_drafting", meta={"doc_ids": [r["filename"] for r in rows]})

    draft = core.agent_say(personas.CANDIDATE,
        f"Draft Chapter {n} of your dissertation on '{state['topic']}'. Ground every empirical claim in the "
        f"cited Behna documents below. ~700 words.\n\nSOURCES:\n{core.format_sources(rows)}", max_tokens=3500)

    review = ""
    approved = False
    for rev in range(MAX_CHAPTER_REVISIONS + 1):
        core.log_event(rid, "candidate", "message", f"Chapter {n} draft (rev {rev})", draft, phase="phase4_drafting")
        review = core.agent_say(personas.ADVISOR,
            f"Review draft Chapter {n}:\n\n{draft}\n\nCheck citations map to real Behna doc IDs. Remember your VERDICT line.")
        approved = _approved(review)
        core.log_event(rid, "advisor", "gate", f"Advisor on chapter {n} (rev {rev})", review,
                       phase="phase4_drafting", meta={"approved": approved})
        if approved or rev == MAX_CHAPTER_REVISIONS:
            break
        _control_gate(rid)
        draft = core.agent_say(personas.CANDIDATE,
            f"Revise Chapter {n} per the advisor's critique:\n\nCRITIQUE:\n{review}\n\nYOUR DRAFT:\n{draft}",
            max_tokens=3500)

    core.save_artifact(rid, "chapter", f"Chapter {n}", draft, version=n, approved=approved)
    state.setdefault("chapters", []).append({"n": n, "text": draft, "approved": approved})
    return state


def route_chapter(state: LabState) -> str:
    if state.get("current_chapter", 0) >= N_CHAPTERS:
        return "colloquium"
    if state.get("current_chapter", 0) == 1 and not state.get("colloquium_done"):
        return "colloquium"   # present the first finished chapter mid-drafting
    return "chapter"


def node_colloquium(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    state["colloquium_done"] = True
    core.set_run(rid, phase="phase5_colloquium")
    core.log_event(rid, "system", "status", "Phase 5 — Conference Colloquium", phase="phase5_colloquium")

    chapter = state["chapters"][-1]["text"]
    core.log_event(rid, "candidate", "message", "Presents chapter to colloquium", chapter[:1500], phase="phase5_colloquium")

    critiques = []
    for lens in personas.PEER_LENSES:
        crit = core.agent_say(personas.PEER.format(lens=lens),
            f"The candidate presents this chapter on '{state['topic']}':\n\n{chapter}\n\nDeliver your critique.")
        critiques.append(crit)
        core.log_event(rid, "peer", "message", f"Peer critique — {lens.split(' — ')[0]}", crit, phase="phase5_colloquium")

    response = core.agent_say(personas.CANDIDATE,
        "Respond to the colloquium and revise your chapter to address the strongest objections:\n\n"
        + "\n\n---\n\n".join(critiques) + f"\n\nYOUR CHAPTER:\n{chapter}", max_tokens=3500)
    core.log_event(rid, "candidate", "message", "Candidate's revised chapter post-colloquium", response, phase="phase5_colloquium")
    state["chapters"][-1]["text"] = response
    core.save_artifact(rid, "chapter_revised", f"Chapter {state['chapters'][-1]['n']} (post-colloquium)", response)

    # If we presented after chapter 1, go finish remaining chapters; else synthesize
    return state


def route_colloquium(state: LabState) -> str:
    if state.get("current_chapter", 0) >= N_CHAPTERS:
        return "thesis"
    return "chapter"


def node_thesis(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase6_thesis")
    core.log_event(rid, "system", "status", "Phase 6 — Final Synthesis", phase="phase6_thesis")

    body = "\n\n".join(f"## Chapter {c['n']}\n\n{c['text']}" for c in state.get("chapters", []))
    thesis = core.agent_say(personas.CANDIDATE,
        f"Synthesize your chapters into a cohesive concluding synthesis for your dissertation on '{state['topic']}'. "
        "Write an abstract, then a macro-level synthesis that ties the chapters together and states your overall "
        f"contribution. ~900 words.\n\nCHAPTERS:\n{body}", max_tokens=4000)
    full = f"# {state['topic']}\n\n*Behna Archive Virtual Lab — synthesized dissertation*\n\n{thesis}\n\n---\n\n{body}"
    state["thesis"] = full
    core.save_artifact(rid, "thesis", "Final Synthesized Thesis", full, approved=True)
    core.log_event(rid, "candidate", "artifact", "Final thesis synthesized", thesis, phase="phase6_thesis")
    core.set_run(rid, phase="completed", status="completed")
    return state


def build_graph():
    g = StateGraph(LabState)
    g.add_node("pitch", node_pitch)
    g.add_node("proposal", node_proposal)
    g.add_node("archive", node_archive)
    g.add_node("chapter", node_chapter)
    g.add_node("colloquium", node_colloquium)
    g.add_node("thesis", node_thesis)

    g.set_entry_point("pitch")
    g.add_edge("pitch", "proposal")
    g.add_conditional_edges("proposal", route_proposal,
                            {"proposal": "proposal", "archive": "archive"})
    g.add_edge("archive", "chapter")
    g.add_conditional_edges("chapter", route_chapter,
                            {"chapter": "chapter", "colloquium": "colloquium"})
    g.add_conditional_edges("colloquium", route_colloquium,
                            {"chapter": "chapter", "thesis": "thesis"})
    g.add_edge("thesis", END)
    return g.compile()
