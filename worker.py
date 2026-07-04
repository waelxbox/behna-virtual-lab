"""Run a single Behna Lab simulation. Usage: python worker.py <run_id>

Reads per-run persona configuration from lab.runs.config and injects it into the
graph via module-level overrides before execution."""
import sys, json, traceback
import core, personas
from graph import build_graph

def run(run_id: int):
    conn = core.db(); cur = conn.cursor()
    cur.execute("SELECT topic, config FROM lab.runs WHERE id=%s", (run_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        print("no such run", run_id); return
    topic, config = row[0], row[1] or {}
    if isinstance(config, str):
        config = json.loads(config)

    # Override module-level persona prompts for this run
    if config.get("candidate"):
        personas.CANDIDATE = config["candidate"]
    if config.get("advisor"):
        personas.ADVISOR = config["advisor"]
    if config.get("peer_template"):
        personas.PEER = config["peer_template"]
    if config.get("peer_lenses"):
        personas.PEER_LENSES = config["peer_lenses"]

    # Override chapter count
    import graph as g
    if config.get("n_chapters"):
        g.N_CHAPTERS = int(config["n_chapters"])

    core.set_run(run_id, status="running", control="run")
    core.log_event(run_id, "system", "status", "Simulation started", f"Topic: {topic}")
    graph = build_graph()
    try:
        graph.invoke({"run_id": run_id, "topic": topic},
                     config={"recursion_limit": 100})
    except RuntimeError as e:
        if "STOPPED_BY_USER" in str(e):
            core.set_run(run_id, status="stopped")
            core.log_event(run_id, "system", "status", "Simulation stopped by user")
            return
        core.set_run(run_id, status="error")
        core.log_event(run_id, "system", "status", "Error", str(e))
        traceback.print_exc()
    except Exception as e:
        core.set_run(run_id, status="error")
        core.log_event(run_id, "system", "status", "Error", str(e))
        traceback.print_exc()

if __name__ == "__main__":
    run(int(sys.argv[1]))
