"""The Behna Virtual Lab: a LangGraph state machine simulating the PhD lifecycle.

Phases:
  0 explore      Candidate browses a random sample of the archive and discovers a research interest.
  1 pitch        Candidate pitches a topic; Advisor accepts/refines.
  2 proposal     Iterative proposal loop, gated by Advisor [APPROVED].
  3 outline      Candidate outlines the chapter structure.
  4 archive      Candidate runs RAG queries, builds annotated bibliography of primary sources.
  5 drafting     Chapter-by-chapter; each chapter gated by Advisor [APPROVED].
  6 colloquium   One chapter defended before adversarial Peer panel; revised.
  7 thesis       Final synthesis of approved chapters into a cohesive document.

Deterministic guardrails use LangGraph conditional edges keyed on the Advisor's
explicit VERDICT flag. The worker also honors UI control signals (pause/stop).
"""
import time, re
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END

import core, personas

MAX_PITCH_ITERS = 2
MAX_PROPOSAL_ITERS = 3
N_CHAPTERS = 3            # kept modest for a tractable run; raise for full dissertation
MAX_CHAPTER_REVISIONS = 2


class LabState(TypedDict, total=False):
    run_id: int
    topic: str
    pitch: str
    pitch_iter: int
    proposal: str
    proposal_iter: int
    outline: str
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
def node_explore(state: LabState) -> LabState:
    """Phase 0: The candidate browses a random sample of the archive and
    organically discovers a research interest before pitching anything."""
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase0_explore", status="running")
    core.log_event(rid, "system", "status", "Phase 0 — Archival Exploration", phase="phase0_explore")

    # Pull a diverse random sample from the corpus
    docs = core.random_sample(k=20)
    src_block = core.format_sources(docs)
    doc_ids = [d["filename"] for d in docs]
    core.log_event(rid, "system", "retrieval", "Random archival sample (20 documents)",
                   src_block, phase="phase0_explore", meta={"doc_ids": doc_ids})

    # Candidate reads and reflects
    nudge = state.get("topic", "")
    nudge_line = ""
    if nudge:
        nudge_line = f"\n\n(You have a loose thematic interest in: '{nudge}' — but let the documents surprise you.)"

    reflection = core.agent_say(personas.CANDIDATE,
        f"You have been given access to the Behna Archives for the first time. Below is a random sample of 20 documents "
        f"from the collection. Read through them carefully. Notice patterns, curiosities, gaps, and contradictions. "
        f"What strikes you? What questions emerge? What stories are hiding in this material?{nudge_line}\n\n"
        f"DOCUMENTS:\n{src_block}\n\n"
        f"Write a 2-3 paragraph reflection on what you found interesting, what patterns you noticed, "
        f"and what research question is beginning to form in your mind. Do NOT pitch a formal topic yet — "
        f"just think aloud as a curious researcher encountering this material for the first time.")
    core.log_event(rid, "candidate", "message", "Archival exploration notes", reflection, phase="phase0_explore")
    core.save_artifact(rid, "exploration", "Archival Exploration Notes", reflection)

    # Now the candidate crystallizes a topic from their exploration
    topic = core.agent_say(personas.CANDIDATE,
        f"Based on your exploration notes below, formulate a single clear research topic (1-2 sentences) "
        f"that you want to investigate further. This will become the seed for your doctoral pitch.\n\n"
        f"YOUR NOTES:\n{reflection}")
    state["topic"] = topic.strip()
    core.log_event(rid, "candidate", "message", "Emergent research interest", topic, phase="phase0_explore")

    return state


def node_pitch(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    it = state.get("pitch_iter", 0) + 1
    state["pitch_iter"] = it
    core.set_run(rid, phase="phase1_pitch", status="running")
    core.log_event(rid, "system", "status", f"Phase 1 — Application & Conceptualization (attempt {it})", phase="phase1_pitch")

    prior_pitch = state.get("pitch", "")
    prompt = (f"Pitch a focused doctoral research question on the Behna Archives related to: '{state['topic']}'. "
              "2-3 paragraphs: the question, why it matters, and the angle your theoretical lens brings.")
    if prior_pitch:
        prompt += (f"\n\nYour previous pitch was rejected. The advisor's critique is below; "
                   f"rework your pitch to address their concerns.\n\nPREVIOUS PITCH:\n{prior_pitch}")

    pitch = core.agent_say(personas.CANDIDATE, prompt)
    state["pitch"] = pitch
    core.log_event(rid, "candidate", "message", f"Research pitch (attempt {it})", pitch, phase="phase1_pitch")

    review = core.agent_say(personas.ADVISOR,
        f"The candidate pitched:\n\n{pitch}\n\nEvaluate the potential of this doctoral topic and either accept "
        "the student (refining the question) or send them back. Suggest theoretical refinements and readings. Remember your VERDICT line.")
    ok = _approved(review)
    core.log_event(rid, "advisor", "gate", f"Advisor on pitch (attempt {it})", review, phase="phase1_pitch",
                   meta={"approved": ok})
    state["_pitch_ok"] = ok
    return state


def route_pitch(state: LabState) -> str:
    if state.get("_pitch_ok"):
        return "proposal"
    if state.get("pitch_iter", 0) >= MAX_PITCH_ITERS:
        return "proposal"   # advisor's critique stands; proceed to keep sim moving
    return "pitch"


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
        prompt += f"\n\nYour previous version and the advisor's critique are below; revise accordingly. Actively incorporate the feedback.\n\nPREVIOUS:\n{prior}"
    proposal = core.agent_say(personas.CANDIDATE, prompt, max_tokens=3000)
    state["proposal"] = proposal
    core.log_event(rid, "candidate", "message", f"Proposal v{it}", proposal, phase="phase2_proposal")
    core.save_artifact(rid, f"proposal_v{it}", f"Proposal v{it}", proposal, version=it)

    review = core.agent_say(personas.ADVISOR,
        f"Review proposal Version {it}:\n\n{proposal}\n\nApply doctoral standards. Suggest readings if necessary. Remember your VERDICT line.")
    ok = _approved(review)
    core.log_event(rid, "advisor", "gate", f"Advisor on proposal v{it}", review,
                   phase="phase2_proposal", meta={"approved": ok})
    state["_proposal_ok"] = ok
    return state


def route_proposal(state: LabState) -> str:
    if state.get("_proposal_ok"):
        return "outline"
    if state.get("proposal_iter", 0) >= MAX_PROPOSAL_ITERS:
        return "outline"   # advisor's last critique stands; proceed to keep sim moving
    return "proposal"


def node_outline(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase3_outline")
    core.log_event(rid, "system", "status", "Phase 3 — Structural Outline", phase="phase3_outline")

    outline = core.agent_say(personas.CANDIDATE,
        f"Based on your proposal on '{state['topic']}', write a structural outline for your dissertation. "
        f"Define the argument arc across {N_CHAPTERS} chapters. Chapter 1 MUST be framed as a literature review and theoretical framing chapter.")
    state["outline"] = outline
    core.log_event(rid, "candidate", "message", "Dissertation Outline", outline, phase="phase3_outline")
    core.save_artifact(rid, "outline", "Dissertation Outline", outline)
    return state


def node_archive(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase4_archive")
    core.log_event(rid, "system", "status", "Phase 4 — Archival Deep-Dive (RAG)", phase="phase4_archive")

    # Candidate formulates search queries
    qtext = core.agent_say(personas.CANDIDATE,
        f"Based on your outline on '{state['topic']}', list exactly 5 concrete search queries "
        "you would run against the Behna archive to find primary evidence. One per line, no numbering.")
    queries = [q.strip("-• ").strip() for q in qtext.splitlines() if q.strip()][:5]
    core.log_event(rid, "candidate", "message", "Archive search queries", "\n".join(queries), phase="phase4_archive")

    all_sources = []
    for q in queries:
        # Increase k to get a broader evidence base
        rows = core.retrieve(q, k=8)
        all_sources.extend(rows)
        core.log_event(rid, "system", "retrieval", f"Retrieved for: {q}",
                       core.format_sources(rows), phase="phase4_archive",
                       meta={"query": q, "doc_ids": [r["filename"] for r in rows]})

    src_block = core.format_sources(all_sources[:20])
    biblio = core.agent_say(personas.CANDIDATE,
        f"Here are primary documents retrieved from the Behna archive:\n\n{src_block}\n\n"
        "Write an annotated bibliography of the most relevant PRIMARY sources (cite each by its Doc filename ID), "
        "noting what each contributes to your argument.", max_tokens=4000)
    state["bibliography"] = biblio
    core.log_event(rid, "candidate", "message", "Annotated primary-source bibliography", biblio, phase="phase4_archive")
    core.save_artifact(rid, "bibliography", "Annotated Primary-Source Bibliography", biblio)
    state["chapters"] = []
    state["current_chapter"] = 0
    return state


def node_chapter(state: LabState) -> LabState:
    rid = state["run_id"]; _control_gate(rid)
    n = state.get("current_chapter", 0) + 1
    state["current_chapter"] = n
    core.set_run(rid, phase="phase5_drafting")
    core.log_event(rid, "system", "status", f"Phase 5 — Drafting Chapter {n}", phase="phase5_drafting")

    # fresh retrieval to ground the chapter
    rows = core.retrieve(f"{state['topic']} chapter {n} evidence", k=10)
    core.log_event(rid, "system", "retrieval", f"Chapter {n} evidence", core.format_sources(rows),
                   phase="phase5_drafting", meta={"doc_ids": [r["filename"] for r in rows]})

    ch_instructions = f"Draft Chapter {n} of your dissertation on '{state['topic']}' based on your outline."
    if n == 1:
        ch_instructions += " Chapter 1 must be a close engagement with the secondary literature to frame and focus the research."
    else:
        ch_instructions += " Ground every empirical claim in the cited Behna documents."

    draft = core.agent_say(personas.CANDIDATE,
        f"{ch_instructions} ~900 words.\n\nSOURCES:\n{core.format_sources(rows)}", max_tokens=4000)

    review = ""
    approved = False
    for rev in range(MAX_CHAPTER_REVISIONS + 1):
        core.log_event(rid, "candidate", "message", f"Chapter {n} draft (rev {rev})", draft, phase="phase5_drafting")
        review = core.agent_say(personas.ADVISOR,
            f"Review draft Chapter {n}:\n\n{draft}\n\nCheck citations map to real Behna doc IDs. Provide constructive mentorship and point out dead ends. Remember your VERDICT line.")
        approved = _approved(review)
        core.log_event(rid, "advisor", "gate", f"Advisor on chapter {n} (rev {rev})", review,
                       phase="phase5_drafting", meta={"approved": approved})
        if approved or rev == MAX_CHAPTER_REVISIONS:
            break
        _control_gate(rid)
        draft = core.agent_say(personas.CANDIDATE,
            f"Revise Chapter {n} per the advisor's critique:\n\nCRITIQUE:\n{review}\n\nYOUR DRAFT:\n{draft}",
            max_tokens=4000)

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
    core.set_run(rid, phase="phase6_colloquium")
    core.log_event(rid, "system", "status", "Phase 6 — Conference Colloquium", phase="phase6_colloquium")

    chapter = state["chapters"][-1]["text"]
    core.log_event(rid, "candidate", "message", "Presents chapter to colloquium", chapter[:1500], phase="phase6_colloquium")

    critiques = []
    for lens in personas.PEER_LENSES:
        crit = core.agent_say(personas.PEER.format(lens=lens),
            f"The candidate presents this chapter on '{state['topic']}':\n\n{chapter}\n\nDeliver your critique.")
        critiques.append(crit)
        core.log_event(rid, "peer", "message", f"Peer critique — {lens.split(' — ')[0]}", crit, phase="phase6_colloquium")

    response = core.agent_say(personas.CANDIDATE,
        "Respond to the colloquium and revise your chapter to address the strongest objections:\n\n"
        + "\n\n---\n\n".join(critiques) + f"\n\nYOUR CHAPTER:\n{chapter}", max_tokens=4000)
    core.log_event(rid, "candidate", "message", "Candidate's revised chapter post-colloquium", response, phase="phase6_colloquium")
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
    core.set_run(rid, phase="phase7_thesis")
    core.log_event(rid, "system", "status", "Phase 7 — Final Synthesis", phase="phase7_thesis")

    body = "\n\n".join(f"## Chapter {c['n']}\n\n{c['text']}" for c in state.get("chapters", []))
    
    # Force a true synthesis instead of just a concatenation
    thesis = core.agent_say(personas.CANDIDATE,
        f"Synthesize your findings from the {N_CHAPTERS} chapters into a cohesive, newly written concluding document for your dissertation on '{state['topic']}'. "
        "Do not just copy-paste the chapters. Write an abstract, an introduction, and a macro-level synthesis that integrates the findings from the data and states your overall contribution. "
        f"~1500 words.\n\nCHAPTERS FOR REFERENCE:\n{body}", max_tokens=8000)
    
    full = f"# {state['topic']}\n\n*Behna Archive Virtual Lab — synthesized dissertation*\n\n{thesis}"
    state["thesis"] = full
    core.save_artifact(rid, "thesis", "Final Synthesized Thesis", full, approved=True)
    core.log_event(rid, "candidate", "artifact", "Final thesis synthesized", thesis, phase="phase7_thesis")
    core.set_run(rid, phase="completed", status="completed")
    return state


def build_graph():
    g = StateGraph(LabState)
    g.add_node("explore", node_explore)
    g.add_node("pitch", node_pitch)
    g.add_node("proposal", node_proposal)
    g.add_node("outline", node_outline)
    g.add_node("archive", node_archive)
    g.add_node("chapter", node_chapter)
    g.add_node("colloquium", node_colloquium)
    g.add_node("thesis", node_thesis)

    g.set_entry_point("explore")
    g.add_edge("explore", "pitch")
    g.add_conditional_edges("pitch", route_pitch,
                            {"pitch": "pitch", "proposal": "proposal"})
    g.add_conditional_edges("proposal", route_proposal,
                            {"proposal": "proposal", "outline": "outline"})
    g.add_edge("outline", "archive")
    g.add_edge("archive", "chapter")
    g.add_conditional_edges("chapter", route_chapter,
                            {"chapter": "chapter", "colloquium": "colloquium"})
    g.add_conditional_edges("colloquium", route_colloquium,
                            {"chapter": "chapter", "thesis": "thesis"})
    g.add_edge("thesis", END)
    return g.compile()
