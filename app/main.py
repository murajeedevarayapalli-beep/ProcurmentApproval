import json, logging, time
from datetime import date, datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from app.config import settings
from app.models import DecisionInput, PurchaseRequest
from app.store import init_db, get_request, list_requests, events, event, write_decision, get_decision_by_key, connect
from app.retrieval import ensure_index
from app.workflow import analyze

logging.basicConfig(level=settings.log_level.upper(),format="%(asctime)s %(levelname)s %(name)s %(message)s")
log=logging.getLogger("navigator.api")
ROOT=Path(__file__).resolve().parent.parent

@asynccontextmanager
async def lifespan(app):
    init_db()
    try: ensure_index()
    except Exception: log.exception("vector index unavailable; lexical retrieval remains available")
    yield

app=FastAPI(title="Agentic Procurement Approval Navigator",version="1.0.0",description="Human-gated procurement workflow demo",lifespan=lifespan)
app.mount("/static",StaticFiles(directory=str(ROOT/"static")),name="static")

def identity(x_user_id: str|None,x_user_role: str|None):
    return x_user_id or "demo-requester",x_user_role or "requester"

@app.middleware("http")
async def observe(request:Request,call_next):
    start=time.perf_counter(); response=await call_next(request)
    log.info("http_request method=%s path=%s status=%s duration_ms=%.1f",request.method,request.url.path,response.status_code,(time.perf_counter()-start)*1000)
    return response

@app.get("/",response_class=HTMLResponse)
def home():return (ROOT/"static/index.html").read_text(encoding="utf-8")

def observability_page():return (ROOT/"static/observability.html").read_text(encoding="utf-8")

@app.get("/logs",response_class=HTMLResponse)
def logs_page():return observability_page()

@app.get("/metrics",response_class=HTMLResponse)
def metrics_page():return observability_page()

@app.get("/traces",response_class=HTMLResponse)
def traces_page():return observability_page()

@app.get("/drifts",response_class=HTMLResponse)
def drifts_page():return observability_page()

def visible_event_rows(user,role):
    with connect() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1000")]
    if role in {"approver","procurement","finance","admin"}:return rows
    visible={r["request_id"] for r in list_requests() if r.get("requester_id")==user}
    return [r for r in rows if r["request_id"] in visible]

@app.get("/api/observability/logs")
def observability_logs(x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role); rows=visible_event_rows(user,role); output=[]
    for row in rows:
        try: payload=json.loads(row["payload"])
        except (TypeError,json.JSONDecodeError): payload={}
        kind=row["event_type"]
        level="ERROR" if kind in {"analysis_failed","workflow_node_failed"} else "WARN" if kind=="decision_recorded" else "INFO"
        if kind=="analysis_completed": message=f"Analysis completed with status: {payload.get('status','unknown')}"
        elif kind=="decision_recorded": message=f"Human decision recorded: {payload.get('decision','recorded')}"
        elif kind.startswith("workflow_node_"): message=f"Workflow node {payload.get('node','unknown')} {kind.removeprefix('workflow_node_')}"
        else: message=kind.replace("_"," ").capitalize()
        output.append({"id":row["id"],"timestamp":row["created_at"],"level":level,"status":payload.get("status","EVENT"),"service":"procurement-navigator","request_id":row["request_id"],"trace_id":payload.get("trace_id"),"message":message,"latency_ms":payload.get("duration_ms")})
    return {"logs":output,"total":len(output),"note":"Application audit/workflow events; platform and infrastructure logs remain in the Vercel dashboard."}

@app.get("/api/health")
def health():return {"status":"ok","ai_mode":settings.ai_mode,"storage":"sqlite","vector_store":"chroma"}

@app.get("/api/requests")
def requests(x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role)
    items=list_requests()
    if role not in {"approver","procurement","finance","admin"}:items=[x for x in items if x["requester_id"]==user]
    return items

@app.post("/api/requests")
def create_request(body:PurchaseRequest,x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role)
    if role not in {"requester","admin"}:raise HTTPException(403,"Role cannot submit purchase requests")
    if body.requester_id!=user and role!="admin":raise HTTPException(403,"Cannot create a request for another user")
    now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    with connect() as db:
        try:db.execute("INSERT INTO requests(request_id,payload,status,created_at,updated_at) VALUES(?,?,'new',?,?)",(body.request_id,body.model_dump_json(),now,now))
        except Exception:raise HTTPException(409,"Request ID already exists")
    event(body.request_id,"request_created",user,body.model_dump(mode="json"));return {"request_id":body.request_id,"status":"new"}

@app.get("/api/requests/{request_id}")
def request_detail(request_id:str,x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role); r=get_request(request_id)
    if not r:raise HTTPException(404,"Request not found")
    if role not in {"approver","procurement","finance","admin"} and r["requester_id"]!=user:raise HTTPException(403,"Not authorized for this request")
    ev=events(request_id)
    analyses=[json.loads(x["payload"]) for x in ev if x["event_type"]=="analysis_completed"]
    decisions=[json.loads(x["payload"]) for x in ev if x["event_type"]=="decision_recorded"]
    return {"request":r,"analysis":analyses[-1] if analyses else None,"decision":decisions[-1] if decisions else None,"history":ev}

@app.post("/api/requests/analyze")
def analyze_request(body:dict,x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role); rid=body.get("request_id")
    r=get_request(rid) if rid else None
    if not r:raise HTTPException(404,"Request not found")
    if role not in {"approver","procurement","finance","admin"} and r["requester_id"]!=user:raise HTTPException(403,"Not authorized for this request")
    try:return analyze(r,user,role)
    except Exception as exc:
        log.exception("analysis_failed request_id=%s",rid)
        raise HTTPException(500,"Analysis failed safely. Inspect audit history and retry.")

@app.post("/api/requests/{request_id}/decision")
def decide(request_id:str,body:DecisionInput,x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role)
    if role not in {"approver","department_manager","department_director","department_vp","procurement_director","finance_controller","security_officer","admin"}:raise HTTPException(403,"An authorized approver role is required")
    r=get_request(request_id)
    if not r:raise HTTPException(404,"Request not found")
    existing=get_decision_by_key(body.idempotency_key)
    if existing:
        if existing["request_id"]!=request_id or existing["approver_id"]!=user:raise HTTPException(409,"Idempotency key was already used for another action")
        return existing
    if r["status"] not in {"pending_human_review","blocked"}:raise HTTPException(409,"Run analysis before recording a decision")
    try:result=write_decision(request_id,user,body.decision,body.rationale,body.idempotency_key)
    except Exception:raise HTTPException(409,"Decision write conflict; retry with a new idempotency key")
    event(request_id,"decision_recorded",user,result)
    return result

@app.get("/api/evaluations")
def evaluations():
    with connect() as db: rows=[json.loads(r[0]) for r in db.execute("SELECT payload FROM evaluations ORDER BY scenario_id")]
    return {"note":"Scenario expectations define a validation plan; they are not measured production performance.","scenarios":rows,"metrics_to_track":["policy_compliance","approver_routing_accuracy","calculation_correctness","evidence_coverage","cycle_time","override_rate"]}

@app.get("/api/metrics")
def metrics():
    with connect() as db:
        statuses={r[0]:r[1] for r in db.execute("SELECT status,count(*) FROM requests GROUP BY status")}
        decisions=db.execute("SELECT count(*) FROM decisions").fetchone()[0]
        events_total=db.execute("SELECT count(*) FROM events").fetchone()[0]
        request_total=db.execute("SELECT count(*) FROM requests").fetchone()[0]
        analysis_total=db.execute("SELECT count(*) FROM events WHERE event_type='analysis_completed'").fetchone()[0]
        failed_total=db.execute("SELECT count(*) FROM events WHERE event_type='analysis_failed'").fetchone()[0]
    rows=list(reversed(visible_event_rows("", "admin")))
    starts={}; durations=[]
    for row in rows:
        try: payload=json.loads(row["payload"])
        except (TypeError,json.JSONDecodeError): payload={}
        if row["event_type"]=="analysis_started" and payload.get("trace_id"): starts[payload["trace_id"]]=row["created_at"]
        elif row["event_type"]=="analysis_completed" and payload.get("trace_id") in starts:
            try: durations.append((datetime.fromisoformat(row["created_at"])-datetime.fromisoformat(starts[payload["trace_id"]])).total_seconds()*1000)
            except (ValueError,TypeError): pass
    return {"request_total":request_total,"request_counts_by_status":statuses,"decision_count":decisions,"audit_event_count":events_total,"analysis_count":analysis_total,"analysis_failure_count":failed_total,"average_analysis_latency_ms":round(sum(durations)/len(durations),2) if durations else None,"production_metrics":"Cycle time, compliance, routing accuracy, calculation correctness, evidence coverage, and override rate require labeled runs and are not inferred from demo data."}

@app.get("/api/observability/traces")
def observability_traces(x_user_id:str|None=Header(None),x_user_role:str|None=Header(None)):
    user,role=identity(x_user_id,x_user_role); rows=list(reversed(visible_event_rows(user,role))); traces={}
    for row in rows:
        try: payload=json.loads(row["payload"])
        except (TypeError,json.JSONDecodeError): payload={}
        trace_id=payload.get("trace_id")
        if not trace_id:continue
        trace=traces.setdefault(trace_id,{"trace_id":trace_id,"request_id":row["request_id"],"started_at":None,"completed_at":None,"status":"RUNNING","spans":[]})
        kind=row["event_type"]
        if kind=="analysis_started":trace["started_at"]=row["created_at"]
        elif kind=="analysis_completed":trace["completed_at"]=row["created_at"];trace["status"]=payload.get("status","COMPLETED")
        elif kind=="analysis_failed":trace["completed_at"]=row["created_at"];trace["status"]="FAILED"
        elif kind in {"workflow_node_completed","workflow_node_failed"}:
            trace["spans"].append({"node":payload.get("node","unknown"),"status":"FAILED" if kind.endswith("failed") else "COMPLETED","timestamp":row["created_at"],"duration_ms":payload.get("duration_ms"),"error":payload.get("error")})
    result=list(traces.values())
    for trace in result:
        try:
            if trace["started_at"] and trace["completed_at"]:trace["duration_ms"]=round((datetime.fromisoformat(trace["completed_at"])-datetime.fromisoformat(trace["started_at"])).total_seconds()*1000,2)
            else:trace["duration_ms"]=None
        except (ValueError,TypeError):trace["duration_ms"]=None
    return {"traces":list(reversed(result)),"total":len(result),"note":"Traces include recorded LangGraph node spans for analyses run after this instrumentation was added."}

@app.get("/api/observability/drifts")
def observability_drifts():
    today=date.today(); findings=[]
    for filename,kind in [("suppliers.json","supplier"),("contracts.json","contract"),("policies.json","policy")]:
        try: records=json.loads((ROOT/"data"/"source"/filename).read_text(encoding="utf-8"))
        except (OSError,json.JSONDecodeError):continue
        for item in records:
            expiry=item.get("expiry_date"); days=None
            if expiry:
                try:days=(date.fromisoformat(expiry)-today).days
                except ValueError:pass
            reasons=[]
            if kind=="supplier":
                if item.get("evidence_status") not in (None,"current"):reasons.append(f"Supplier evidence is {item['evidence_status']}.")
                if item.get("sanctions_status") not in (None,"clear"):reasons.append(f"Sanctions status is {item['sanctions_status']}.")
                if item.get("status") not in (None,"approved"):reasons.append(f"Supplier status is {item['status']}.")
            if days is not None and days<0:reasons.append(f"Record expired {-days} days ago.")
            elif days is not None and days<=30:reasons.append(f"Record expires in {days} days.")
            if reasons:findings.append({"source_type":kind,"source_id":item.get("source_id") or item.get("supplier_id","unknown"),"title":item.get("title") or item.get("name",item.get("source_id","Evidence record")),"owner":item.get("owner","Unspecified"),"version":item.get("version"),"expiry_date":expiry,"severity":"high" if (days is not None and days<0) or kind=="supplier" and item.get("evidence_status") in {"stale","incomplete"} else "warning","findings":reasons})
    return {"as_of":today.isoformat(),"signals":findings,"total":len(findings),"note":"These are source freshness and expiry signals from the current files, not statistical model/data drift. Historical snapshots and baselines are not configured."}
