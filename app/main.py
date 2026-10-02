import json, logging, time
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
        event(rid,"analysis_failed",user,{"error":str(exc)[:300]})
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
    return {"request_counts_by_status":statuses,"decision_count":decisions,"audit_event_count":events_total,"production_metrics":"Cycle time, compliance, routing accuracy, calculation correctness, evidence coverage, and override rate require labeled runs and are not inferred from demo data."}
