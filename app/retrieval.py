import re, math
from datetime import date
from pathlib import Path
import chromadb
from app.config import settings
from app.store import DATA, all_evidence, lexical_search

def embed(text: str, dims=384):
    # Small deterministic signed feature hashing keeps the local vector index offline and reproducible.
    tokens=re.findall(r"[a-z0-9]+",text.lower())
    v=[0.0]*dims
    for token in tokens:
        h=hashlib_sha(token)
        i=h%dims; v[i]+=1 if (h>>16)&1 else -1
    norm=math.sqrt(sum(x*x for x in v)) or 1
    return [x/norm for x in v]

def hashlib_sha(s):
    import hashlib
    return int.from_bytes(hashlib.sha256(s.encode()).digest()[:8],"little")

def ensure_index():
    root=DATA/"chroma"
    root.mkdir(parents=True,exist_ok=True)
    client=chromadb.PersistentClient(path=str(root))
    collection=client.get_or_create_collection(settings.chroma_collection,metadata={"hnsw:space":"cosine"})
    docs=all_evidence()
    if docs and collection.count()==0:
        collection.add(ids=[d["source_id"] for d in docs],documents=[d["title"]+" "+d["content"] for d in docs],metadatas=[{k:str(d.get(k,"")) for k in ["source_id","title","owner","version","effective_date","expiry_date","classification","access_scope"]} for d in docs],embeddings=[embed(d["title"]+" "+d["content"]) for d in docs])
    return collection

def search(query, role="requester", supplier_id=None, limit=8):
    coll=ensure_index()
    candidates={}
    try:
        res=coll.query(query_embeddings=[embed(query)],n_results=min(16,max(1,coll.count())))
        for i,meta in enumerate(res.get("metadatas",[[]])[0]): candidates[meta["source_id"]]=(meta,1/(1+i))
    except Exception: pass
    for i,d in enumerate(lexical_search(query,limit=16)):
        if d["source_id"] not in candidates:candidates[d["source_id"]]=(d,1/(1+i))
        else:
            meta,score=candidates[d["source_id"]]; candidates[d["source_id"]]=(meta,score+1/(1+i))
    allowed={"requester":{"internal","public"},"approver":{"internal","confidential","public"},"finance":{"internal","confidential","public"},"procurement":{"internal","confidential","public"},"risk":{"internal","confidential","public"},"security":{"internal","confidential","public"},"admin":{"internal","confidential","public"}}
    out=[]
    for sid,(meta,score) in candidates.items():
        # Role and evidence scope are checked again against the canonical source record.
        full=next((x for x in all_evidence() if x["source_id"]==sid),None)
        if not full: continue
        scopes=set(full.get("access_scope","").split(","))
        if full.get("classification") not in allowed.get(role,set()): continue
        if role!="admin" and role not in scopes: continue
        if role not in allowed and role not in scopes: continue
        expiry=full.get("expiry_date")
        if expiry and expiry < date.today().isoformat(): full["freshness"]="expired"
        else: full["freshness"]="current"
        content=full.get("content","")
        out.append({"source_id":sid,"title":full["title"],"owner":full["owner"],"version":full["version"],"effective_date":full["effective_date"],"expiry_date":expiry,"classification":full["classification"],"access_scope":full["access_scope"],"excerpt":content[:900],"score":round(score,4),"freshness":full["freshness"]})
    return sorted(out,key=lambda x:(x["freshness"]!="current",-x["score"]))[:limit]
