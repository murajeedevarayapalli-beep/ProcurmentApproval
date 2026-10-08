import json, logging, uuid, time
from typing import TypedDict, Any
from pathlib import Path
from langgraph.graph import StateGraph, END
from app.models import PurchaseRequest
from app.retrieval import search
from app.store import ROOT, event, set_status

log=logging.getLogger("navigator.workflow")

class WorkflowState(TypedDict, total=False):
    request: dict
    actor: str
    role: str
    trace_id: str
    attempts: int
    classification: str
    evidence: list[dict]
    policy_findings: list[str]
    budget_findings: list[str]
    supplier_findings: list[str]
    exceptions: list[str]
    approver: str | None
    status: str
    summary: str
    result: dict

def classify(s):
    r=PurchaseRequest.model_validate(s["request"])
    s["classification"]="restricted_software" if "software" in r.category or "data" in r.description.lower() else r.category
    return s

def retrieve(s):
    r=s["request"]
    q=f"{r['category']} {r['description']} procurement policy budget approval security supplier {r['amount']} {r['supplier_id']}"
    try:
        s["evidence"]=search(q,role=s.get("role","requester"),supplier_id=r["supplier_id"])
        if not s["evidence"]:
            s["attempts"]=1
            s["evidence"]=search(f"{r['category']} procurement delegation competition approval evidence",role=s.get("role","requester"),limit=8)
        if not s["evidence"]:
            s.setdefault("exceptions",[]).append("No accessible source evidence was retrieved; review cannot proceed on unsupported claims.")
    except Exception as ex:
        log.exception("retrieval failed trace_id=%s",s.get("trace_id"))
        s["exceptions"]=["Evidence retrieval failed; human reviewer must inspect source records."]
        s["evidence"]=[]
    return s

def budget_tool(cost_center, amount):
    for x in json.loads((ROOT/"data/source/budgets.json").read_text(encoding="utf-8")):
        if cost_center in x["content"]:
            import re
            m=re.search(re.escape(cost_center)+r" has approved annual budget (\d+) USD, committed spend (\d+) USD, available balance (\d+) USD",x["content"])
            if m:
                approved,committed,available=map(float,m.groups())
                return {"available":available,"requested":amount,"remaining_after":available-amount,"within_budget":amount<=available,"source_id":x["source_id"]}
    return {"available":None,"requested":amount,"remaining_after":None,"within_budget":False,"source_id":None}

def policy_budget(s):
    r=s["request"]; findings=[]; p=[]; exceptions=list(s.get("exceptions",[]))
    if not r.get("description") or not r.get("cost_center"):exceptions.append("Required business purpose or cost center is missing.")
    if r["amount"]>=25000:p.append("Competitive sourcing event and Procurement Director review required (POL-001).")
    elif r["amount"]>=5000:p.append("Two comparable quotes required unless Procurement approves a sole-source exception (POL-001).")
    else:p.append("One approved catalog or documented quote is sufficient under the stated threshold (POL-001).")
    b=budget_tool(r["cost_center"],float(r["amount"]))
    if b["within_budget"]:findings.append(f"Budget available: ${b['available']:,.2f}; after request: ${b['remaining_after']:,.2f} (BUD-001).")
    elif b["available"] is None:exceptions.append("No current budget record found for this cost center.")
    else:exceptions.append(f"Budget shortfall: request ${r['amount']:,.2f}, available ${b['available']:,.2f} (BUD-001).")
    s["policy_findings"]=p; s["budget_findings"]=findings; s["exceptions"]=exceptions; s["budget_result"]=b
    return s

def supplier_tool(s):
    r=s["request"]
    suppliers=json.loads((ROOT/"data/source/suppliers.json").read_text(encoding="utf-8"))
    sp=next((x for x in suppliers if x["supplier_id"]==r["supplier_id"]),None)
    f=[]; ex=list(s.get("exceptions",[]))
    if not sp: ex.append("Supplier record not found.")
    else:
        f.append(f"{sp['name']}: status {sp['status']}, risk {sp['risk_tier']}, evidence {sp['evidence_status']}.")
        if sp["status"]!="approved":ex.append("Supplier is not fully approved; resolve Supplier Operations conditions.")
        if sp["sanctions_status"]!="clear":ex.append("Current sanctions clearance is not confirmed.")
        if sp["evidence_status"]!="current":ex.append("Supplier evidence is stale or incomplete.")
    contracts=json.loads((ROOT/"data/source/contracts.json").read_text(encoding="utf-8"))
    ct=next((x for x in contracts if x.get("supplier_id")==r["supplier_id"]),None)
    if ct:
        from datetime import date
        if ct["expiry_date"]<date.today().isoformat():ex.append(f"Contract {ct['source_id']} is expired; obtain an executed current agreement.")
        else:f.append(f"Contract {ct['source_id']} is current through {ct['expiry_date']}.")
    if "software" in r["category"] or "data" in r["description"].lower():ex.append("Restricted software: current Security review and data-processing assessment are required (POL-002).")
    s["supplier_findings"]=f;s["exceptions"]=list(dict.fromkeys(ex));return s

def route(s):
    amount=float(s["request"]["amount"])
    s["approver"]="department_manager" if amount<5000 else "department_director" if amount<25000 else "department_vp"
    if amount>=25000:s["approver"]+=" + procurement_director + finance_controller"
    if "software" in s["request"]["category"] or "data" in s["request"]["description"].lower():s["approver"]+=" + security_officer"
    s["status"]="blocked" if s.get("exceptions") else "pending_human_review"
    return s

def planning_agent(s):
    """Optional model agent drafts next steps; it has no tools and cannot alter findings or status."""
    s["llm_advice"]=[]
    from app.config import settings
    if settings.ai_mode.lower()!="openai" or not settings.openai_api_key:return s
    try:
        from openai import OpenAI
        client=OpenAI(api_key=settings.openai_api_key)
        payload={"request_id":s["request"]["request_id"],"exceptions":s.get("exceptions",[]),"policy_findings":s.get("policy_findings",[]),"budget_findings":s.get("budget_findings",[]),"supplier_findings":s.get("supplier_findings",[])}
        out=client.chat.completions.create(model=settings.openai_model,temperature=0,response_format={"type":"json_object"},messages=[{"role":"system","content":"You are the procurement exception planning agent. Draft at most 3 concise next-step suggestions as JSON with key suggestions (array of strings). Do not approve, waive policy, claim evidence exists, or change findings. Use only supplied facts."},{"role":"user","content":json.dumps(payload)}])
        parsed=json.loads(out.choices[0].message.content or "{}")
        s["llm_advice"]=[str(x)[:240] for x in parsed.get("suggestions",[])[:3]]
    except Exception:
        log.exception("planner_agent_fallback trace_id=%s",s.get("trace_id"))
    return s

def finish(s):
    r=s["request"]
    exceptions=s.get("exceptions",[])
    status=s.get("status","blocked")
    summary=(f"{r['request_id']} is {status.replace('_',' ')}. "
      f"Deterministic budget check: {s.get('budget_result',{}).get('within_budget',False)}. "
      f"{len(exceptions)} open exception(s); an accountable human must review evidence before any commitment.")
    result={"request_id":r["request_id"],"status":status,"summary":summary,"classification":s.get("classification",r["category"]),"policy_findings":s.get("policy_findings",[]),"budget_findings":s.get("budget_findings",[]),"supplier_findings":s.get("supplier_findings",[]),"exceptions":exceptions,"recommended_approver_role":s.get("approver"),"evidence":s.get("evidence",[]),"next_action":"Resolve all open exceptions and rerun analysis." if status=="blocked" else "Named approver reviews cited evidence and records a decision.","planner_suggestions":s.get("llm_advice",[]),"trace_id":s["trace_id"]}
    s["result"]=result
    set_status(r["request_id"],status)
    event(r["request_id"],"analysis_completed",s.get("actor","system"),result)
    log.info("analysis_complete trace_id=%s request_id=%s status=%s",s["trace_id"],r["request_id"],status)
    return s

def build_graph():
    g=StateGraph(WorkflowState)
    def observed(name, fn):
        def run(state):
            request_id=state.get("request",{}).get("request_id","unknown")
            trace_id=state.get("trace_id","unknown")
            actor=state.get("actor","system")
            started=time.perf_counter()
            event(request_id,"workflow_node_started",actor,{"trace_id":trace_id,"node":name})
            try:
                result=fn(state)
            except Exception as exc:
                event(request_id,"workflow_node_failed",actor,{"trace_id":trace_id,"node":name,"error":str(exc)[:240],"duration_ms":round((time.perf_counter()-started)*1000,2)})
                raise
            event(request_id,"workflow_node_completed",actor,{"trace_id":trace_id,"node":name,"duration_ms":round((time.perf_counter()-started)*1000,2)})
            return result
        return run
    for name,fn in [("classify",classify),("retrieve",retrieve),("policy_budget",policy_budget),("supplier",supplier_tool),("route",route),("planner_agent",planning_agent),("finish",finish)]:g.add_node(name,observed(name,fn))
    g.set_entry_point("classify");g.add_edge("classify","retrieve")
    g.add_edge("retrieve","policy_budget")
    g.add_edge("policy_budget","supplier");g.add_edge("supplier","route");g.add_edge("route","planner_agent");g.add_edge("planner_agent","finish");g.add_edge("finish",END)
    return g.compile()

GRAPH=build_graph()

def analyze(request,actor,role):
    trace_id=str(uuid.uuid4())
    event(request["request_id"],"analysis_started",actor,{"trace_id":trace_id})
    try:
        state=GRAPH.invoke({"request":request,"actor":actor,"role":role,"trace_id":trace_id,"attempts":0,"exceptions":[]})
        return state["result"]
    except Exception as exc:
        event(request["request_id"],"analysis_failed",actor,{"trace_id":trace_id,"error":str(exc)[:240]})
        raise
