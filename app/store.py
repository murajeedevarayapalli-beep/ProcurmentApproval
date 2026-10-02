import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(settings.data_dir)
if not DATA.is_absolute(): DATA = (ROOT / DATA).resolve()
SOURCE = ROOT / "data" / "source"
DB = DATA / "procurement.db"

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS requests(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, approver_id TEXT NOT NULL, decision TEXT NOT NULL, rationale TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(source_id UNINDEXED, title, content, owner, tokenize='porter');
        CREATE TABLE IF NOT EXISTS evidence_meta(source_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evaluations(scenario_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        for filename in ["policies.json", "approval_matrix.json", "budgets.json", "suppliers.json", "contracts.json"]:
            data = json.loads((SOURCE / filename).read_text(encoding="utf-8"))
            for item in data:
                if "content" not in item: continue
                # Supplier master records use supplier_id as their stable source identifier.
                source_id = item.get("source_id") or item.get("supplier_id")
                if not source_id:
                    raise ValueError(f"Evidence record in {filename} is missing source_id")
                item["source_id"] = source_id
                item.setdefault("title", item.get("name") or source_id)
                item.setdefault("owner", "Unspecified source owner")
                db.execute("INSERT OR IGNORE INTO evidence_meta VALUES(?,?)", (source_id, json.dumps(item)))
                db.execute("INSERT OR IGNORE INTO evidence_fts(source_id,title,content,owner) VALUES(?,?,?,?)", (source_id, item["title"], item["content"], item["owner"]))
        for item in json.loads((SOURCE / "requests.json").read_text(encoding="utf-8")):
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT OR IGNORE INTO requests(request_id,payload,status,created_at,updated_at) VALUES(?,?,'new',?,?)", (item["request_id"], json.dumps(item), now, now))
        seeds = [
          {"scenario_id":"EV-001","title":"Catalog office purchase within authority","request_id":"PR-1042","expected_outcome":"pending_human_review","checks":["policy_compliance","approver_routing_accuracy","calculation_correctness","evidence_coverage","cycle_time","override_rate"],"expected_approver_role":"department_manager"},
          {"scenario_id":"EV-002","title":"Restricted software with expired supplier evidence","request_id":"PR-1043","expected_outcome":"blocked","checks":["policy_compliance","evidence_coverage","approver_routing_accuracy"],"expected_approver_role":"department_director"},
          {"scenario_id":"EV-003","title":"Urgent request with open sanctions and budget shortfall","request_id":"PR-1044","expected_outcome":"blocked","checks":["calculation_correctness","policy_compliance","cycle_time"],"expected_approver_role":"department_director"},
          {"scenario_id":"EV-004","title":"Competitive threshold escalation and restricted software","request_id":"PR-1045","expected_outcome":"blocked","checks":["approver_routing_accuracy","policy_compliance","evidence_coverage"],"expected_approver_role":"department_vp"}
        ]
        for e in seeds: db.execute("INSERT OR IGNORE INTO evaluations VALUES(?,?)", (e["scenario_id"], json.dumps(e)))
        db.commit()

def get_request(request_id):
    with connect() as db:
        row=db.execute("SELECT * FROM requests WHERE request_id=?",(request_id,)).fetchone()
        return ({**json.loads(row["payload"]),"status":row["status"]} if row else None)

def list_requests():
    with connect() as db:
        return [{**json.loads(r["payload"]), "status":r["status"], "updated_at":r["updated_at"]} for r in db.execute("SELECT * FROM requests ORDER BY request_id")]

def set_status(request_id,status):
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db: db.execute("UPDATE requests SET status=?,updated_at=? WHERE request_id=?",(status,now,request_id))

def event(request_id,event_type,actor,payload):
    with connect() as db: db.execute("INSERT INTO events(request_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",(request_id,event_type,actor,json.dumps(payload,default=str),datetime.now(timezone.utc).isoformat()))

def events(request_id):
    with connect() as db: return [dict(r) for r in db.execute("SELECT * FROM events WHERE request_id=? ORDER BY id",(request_id,))]

def write_decision(request_id,approver,decision,rationale,key):
    now=datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute("INSERT INTO decisions(request_id,approver_id,decision,rationale,idempotency_key,created_at) VALUES(?,?,?,?,?,?)",(request_id,approver,decision,rationale,key,now))
        db.execute("UPDATE requests SET status=?,updated_at=? WHERE request_id=?",(decision,now,request_id))
    return {"request_id":request_id,"approver_id":approver,"decision":decision,"rationale":rationale,"decided_at":now}

def get_decision_by_key(key):
    with connect() as db:
        r=db.execute("SELECT * FROM decisions WHERE idempotency_key=?",(key,)).fetchone()
        return dict(r) if r else None

def lexical_search(query, limit=8):
    terms=[x for x in query.split() if x.isalnum()]
    if not terms:return []
    with connect() as db:
        try: rows=db.execute("SELECT source_id,title,content,owner,bm25(evidence_fts) rank FROM evidence_fts WHERE evidence_fts MATCH ? ORDER BY rank LIMIT ?",(" OR ".join(terms),limit)).fetchall()
        except sqlite3.OperationalError: return []
        result=[]
        for r in rows:
            meta=db.execute("SELECT payload FROM evidence_meta WHERE source_id=?",(r["source_id"],)).fetchone()
            if meta: result.append(json.loads(meta["payload"]))
        return result

def all_evidence():
    with connect() as db:return [json.loads(r[0]) for r in db.execute("SELECT payload FROM evidence_meta")]
