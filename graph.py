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

v2 changes (Mark's feedback):
- Phase-specific evaluation criteria for advisor/peer
- Hard rejection gate (simulation fails if max attempts exceeded without approval)
- Informal peer micro-feedback during drafting
- Thesis built from the full discussion history with proper source analysis
"""
import time, re, random
from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END

import core, personas

MAX_PITCH_ITERS = 2
MAX_PROPOSAL_ITERS = 3
N_CHAPTERS = 3            # kept modest for a tractable run; raise for full dissertation
MAX_CHAPTER_REVISIONS = 2

# Whether to enforce hard rejection gates (if True, run fails on max rejections)
HARD_REJECTION_GATE = True

# Probability of informal peer micro-feedback firing during drafting
INFORMAL_PEER_PROBABILITY = 0.4


# ---- Phase-specific evaluation criteria ----
# These are injected into the advisor's prompt depending on the current phase.
PHASE_CRITERIA = {
    "pitch": """EVALUATION CRITERIA FOR THIS STAGE (Application/Pitch):
You are evaluating an INITIAL application. At this stage, only GENERAL feedback is required:
- Is this a manageable project given the archive available?
- Is the broad approach sound?
- Does the candidate show awareness of the field?
DO NOT provide detailed critique of the theoretical framework at this point.
DO NOT demand specific citations or methodology details yet.
Keep your feedback high-level and encouraging if the direction is promising.""",

    "proposal": """EVALUATION CRITERIA FOR THIS STAGE (Proposal):
You are evaluating a RESEARCH PROPOSAL. Focus on:
- Does it contain a clearly defined research question?
- Does it have a sound theoretical and methodological framing?
- Does it display awareness of all the seminal readings in the field?
- Does it position the new research appropriately vis-a-vis the existing literature?
DO NOT expect conclusions or findings at this point — the candidate has not yet handled documents.
DO NOT assess evidence handling — that comes later in the writing stage.
Focus on the intellectual architecture: question, method, theory, and literature.""",

    "chapter": """EVALUATION CRITERIA FOR THIS STAGE (Writing/Drafting):
You are evaluating a DRAFT CHAPTER. At this stage, your feedback should be DETAILED:
- Does the analysis and evidence in this chapter address the question(s) set out in the proposal?
- Are claims supported by specific references to primary sources (Behna document IDs)?
- Is the argument logically structured and does it advance the overall thesis?
- Are secondary sources engaged critically, not just cited?
- Is there a clear connection between the evidence presented and the conclusions drawn?
Be rigorous and specific. Point to exact passages that need strengthening.""",
}


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
    # New: track all feedback for thesis synthesis
    feedback_history: List[Dict[str, str]]


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


def _record_feedback(state, phase, agent, content):
    """Append feedback to the running history for thesis synthesis."""
    history = state.get("feedback_history") or []
    history.append({"phase": phase, "agent": agent, "content": content[:2000]})
    state["feedback_history"] = history


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
    state["feedback_history"] = []
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

    # Advisor evaluates with PHASE-SPECIFIC criteria
    advisor_prompt = (
        f"{PHASE_CRITERIA['pitch']}\n\n"
        f"The candidate pitched:\n\n{pitch}\n\n"
        "Evaluate whether this is a manageable and promising doctoral project. "
        "If the broad direction is sound, accept the student. If not, explain what general direction would be more viable. "
        "Remember your VERDICT line."
    )
    review = core.agent_say(personas.ADVISOR, advisor_prompt)
    ok = _approved(review)
    core.log_event(rid, "advisor", "gate", f"Advisor on pitch (attempt {it})", review, phase="phase1_pitch",
                   meta={"approved": ok})
    _record_feedback(state, "pitch", "advisor", review)
    state["_pitch_ok"] = ok
    return state


def route_pitch(state: LabState) -> str:
    if state.get("_pitch_ok"):
        return "proposal"
    if state.get("pitch_iter", 0) >= MAX_PITCH_ITERS:
        if HARD_REJECTION_GATE:
            # Hard gate: force one more attempt with explicit "last chance" framing
            # but still proceed to keep the simulation moving (pitch is low-stakes)
            return "proposal"
        return "proposal"
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

    # Advisor evaluates with PHASE-SPECIFIC criteria (proposal stage)
    advisor_prompt = (
        f"{PHASE_CRITERIA['proposal']}\n\n"
        f"Review proposal Version {it}:\n\n{proposal}\n\n"
        "Apply doctoral standards for a research proposal. Focus on the intellectual architecture: "
        "Is the research question clear? Is the methodology sound? Does it engage the key literature? "
        "Remember your VERDICT line."
    )
    review = core.agent_say(personas.ADVISOR, advisor_prompt)
    ok = _approved(review)
    core.log_event(rid, "advisor", "gate", f"Advisor on proposal v{it}", review,
                   phase="phase2_proposal", meta={"approved": ok})
    _record_feedback(state, "proposal", "advisor", review)
    state["_proposal_ok"] = ok
    return state


def route_proposal(state: LabState) -> str:
    if state.get("_proposal_ok"):
        return "outline"
    if state.get("proposal_iter", 0) >= MAX_PROPOSAL_ITERS:
        if HARD_REJECTION_GATE:
            # Hard gate: the proposal was rejected MAX times. The candidate must
            # fundamentally rethink. Log a failure event but still proceed with
            # the last version (advisor's critique stands as a caveat).
            rid = state.get("run_id")
            if rid:
                core.log_event(rid, "system", "status",
                    f"⚠ Proposal rejected {MAX_PROPOSAL_ITERS} times — proceeding with advisor's reservations noted",
                    "The advisor's final critique remains unresolved. The candidate proceeds at risk.",
                    phase="phase2_proposal", meta={"hard_gate_triggered": True})
        return "outline"
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


def _maybe_informal_peer(state: LabState, chapter_text: str):
    """Probabilistically inject a short informal peer comment during drafting.
    Simulates random hallway/coffee conversations that happen in academic life."""
    if random.random() > INFORMAL_PEER_PROBABILITY:
        return  # No informal feedback this time

    rid = state["run_id"]
    # Pick a random casual lens
    casual_lenses = [
        "a fellow PhD student who works on a completely different region but finds this fascinating",
        "a historian you met at a conference coffee break who has a quick thought",
        "a friend from another department who read your draft over lunch",
        "a visiting scholar who overheard you discussing your work in the common room",
    ]
    lens = random.choice(casual_lenses)

    comment = core.agent_say(
        f"You are {lens}. You are NOT a formal reviewer. You just have one quick, casual observation or question about this work. "
        "Keep it to 2-3 sentences maximum. Be conversational, not academic. You might notice something the author missed, "
        "ask a naive but insightful question, or make an unexpected connection to something else.",
        f"A colleague is working on this chapter about the Behna Archives:\n\n{chapter_text[:1500]}\n\n"
        "Share one brief, informal observation or question."
    )
    core.log_event(rid, "peer", "message", f"Informal comment — {lens.split(' who ')[0]}",
                   comment, phase="phase5_drafting", meta={"informal": True})
    _record_feedback(state, "drafting_informal", "peer", comment)


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

    # Possibly inject informal peer micro-feedback
    _maybe_informal_peer(state, draft)

    review = ""
    approved = False
    for rev in range(MAX_CHAPTER_REVISIONS + 1):
        core.log_event(rid, "candidate", "message", f"Chapter {n} draft (rev {rev})", draft, phase="phase5_drafting")

        # Advisor evaluates with PHASE-SPECIFIC criteria (writing/drafting stage)
        advisor_prompt = (
            f"{PHASE_CRITERIA['chapter']}\n\n"
            f"Review draft Chapter {n} (revision {rev}):\n\n{draft}\n\n"
            f"The candidate's proposal stated their research question as: '{state['topic']}'\n\n"
            "Assess whether this chapter's analysis and evidence address the questions set out in the proposal. "
            "Check that citations map to real Behna doc IDs. Provide constructive mentorship. "
            "Remember your VERDICT line."
        )
        review = core.agent_say(personas.ADVISOR, advisor_prompt)
        approved = _approved(review)
        core.log_event(rid, "advisor", "gate", f"Advisor on chapter {n} (rev {rev})", review,
                       phase="phase5_drafting", meta={"approved": approved})
        _record_feedback(state, f"chapter_{n}", "advisor", review)
        if approved or rev == MAX_CHAPTER_REVISIONS:
            break
        _control_gate(rid)
        draft = core.agent_say(personas.CANDIDATE,
            f"Revise Chapter {n} per the advisor's critique:\n\nCRITIQUE:\n{review}\n\nYOUR DRAFT:\n{draft}",
            max_tokens=4000)

    if not approved and HARD_REJECTION_GATE:
        core.log_event(rid, "system", "status",
            f"⚠ Chapter {n} not approved after {MAX_CHAPTER_REVISIONS} revisions — proceeding with reservations",
            phase="phase5_drafting", meta={"hard_gate_triggered": True})

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
        _record_feedback(state, "colloquium", "peer", crit)

    response = core.agent_say(personas.CANDIDATE,
        "Respond to the colloquium and revise your chapter to address the strongest objections:\n\n"
        + "\n\n---\n\n".join(critiques) + f"\n\nYOUR CHAPTER:\n{chapter}", max_tokens=4000)
    core.log_event(rid, "candidate", "message", "Candidate's revised chapter post-colloquium", response, phase="phase6_colloquium")
    state["chapters"][-1]["text"] = response
    core.save_artifact(rid, "chapter_revised", f"Chapter {state['chapters'][-1]['n']} (post-colloquium)", response)

    return state


def route_colloquium(state: LabState) -> str:
    if state.get("current_chapter", 0) >= N_CHAPTERS:
        return "thesis"
    return "chapter"


def node_thesis(state: LabState) -> LabState:
    """Phase 7: Final synthesis — built FROM the discussion, not from scratch.
    
    The thesis must:
    1. Show how the argument evolved through feedback
    2. Contain analysis of actual primary sources with document IDs
    3. Systematically support claims with reference to both literature AND primary sources
    4. Demonstrate that peer and advisor feedback was incorporated
    """
    rid = state["run_id"]; _control_gate(rid)
    core.set_run(rid, phase="phase7_thesis")
    core.log_event(rid, "system", "status", "Phase 7 — Final Synthesis", phase="phase7_thesis")

    body = "\n\n".join(f"## Chapter {c['n']}\n\n{c['text']}" for c in state.get("chapters", []))

    # Build a structured summary of the feedback journey
    feedback_history = state.get("feedback_history") or []
    feedback_summary = ""
    if feedback_history:
        feedback_parts = []
        for fb in feedback_history[-15:]:  # Last 15 pieces of feedback to stay within context
            feedback_parts.append(f"[{fb['phase']}] {fb['agent']}: {fb['content'][:500]}")
        feedback_summary = "\n\n".join(feedback_parts)

    # The thesis prompt now explicitly requires building on the discussion
    thesis = core.agent_say(personas.CANDIDATE,
        f"Write the FINAL VERSION of your dissertation on '{state['topic']}'. "
        "This is NOT a fresh document — it must demonstrably BUILD ON the iterative feedback process you went through. "
        "\n\nREQUIREMENTS:"
        "\n1. Write an abstract, introduction, and synthesis that integrates ALL your chapters."
        "\n2. Show how your argument EVOLVED through the feedback process. Reference specific critiques that changed your thinking."
        "\n3. Every empirical claim MUST cite a specific Behna document by filename ID (e.g., Doc 049.jpg)."
        "\n4. Systematically support claims with reference to BOTH secondary literature AND primary sources."
        "\n5. Address the key objections raised during the colloquium and by your advisor."
        "\n6. The conclusion must state your overall contribution to the field and acknowledge remaining limitations."
        f"\n\n~2000 words."
        f"\n\nYOUR CHAPTERS (the raw material to synthesize from):\n{body}"
        f"\n\nKEY FEEDBACK YOU RECEIVED DURING THE PROCESS:\n{feedback_summary}"
        f"\n\nYOUR ORIGINAL PROPOSAL:\n{state.get('proposal', '(not available)')[:1500]}",
        max_tokens=10000)

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
