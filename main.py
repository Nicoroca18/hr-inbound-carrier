"""
main.py - Inbound Carrier Agent API (FastAPI)
Realistic negotiation + Dashboard fully smooth (no flicker, no "Loading" message)

Everything updates silently every 5 s
"""

import os, re, json, time, requests
from typing import Optional, List, Dict, Any, Tuple
from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

API_KEY = os.getenv("API_KEY", "test-api-key")
FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY", "")
FMCSA_MODE = os.getenv("FMCSA_MODE", "real").lower()
LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MAX_OVER_PCT = float(os.getenv("MAX_OVER_PCT", "0.10"))
PUBLIC_DASHBOARD = os.getenv("PUBLIC_DASHBOARD", "false").lower() == "true"

app = FastAPI(title="HappyRobot – Inbound Carrier API (Smooth Dashboard)")

negotiations: Dict[str, Dict[str, Any]] = {}
call_results: List[Dict[str, Any]] = []
metrics = {"calls_total":0,"auth_failures":0,"offers_accepted":0,"offers_rejected":0,
            "negotiation_rounds_total":0,"fmcsa_real_calls":0,"fmcsa_real_errors":0,"fmcsa_mock_uses":0}

class CarrierIn(BaseModel): mc_number:str
class LoadOut(BaseModel):
    load_id:str; origin:str; destination:str
    pickup_datetime:str; delivery_datetime:str
    equipment_type:str; loadboard_rate:float
class NegotiateIn(BaseModel): mc_number:str; load_id:str; offer:Any
class CallResultIn(BaseModel): transcript:str; mc_number:Optional[str]=None; load_id:Optional[str]=None
final_price:Optional[Any]=None; accepted:Optional[bool]=None

def require_api_key(x_api_key:str=Header(...)):
    if x_api_key!=API_KEY: raise HTTPException(401,"Invalid x-api-key")

def load_loads():
    if not os.path.exists(LOADS_FILE): return []
    with open(LOADS_FILE,"r",encoding="utf-8") as f: return json.load(f)

def parse_amount(v:Any)->float:
    if isinstance(v,(int,float)): return float(v)
    if isinstance(v,str):
        s=v.strip().replace("$","").replace(",","")
        try: return float(s)
        except: pass
    raise HTTPException(422,"Invalid offer")

price_re=re.compile(r"\b(?:\$)?\s*(\d{2,6}(?:\.\d{1,2})?)\b")
mc_re=re.compile(r"\bMC(?:\s|#|:)?\s*(\d{4,10})\b",re.I)
loadid_re=re.compile(r"\bL\d{3,}\b",re.I)
def extract_entities_from_text(t:str)->Dict[str,Any]:
    e={}; 
    if m:=mc_re.search(t): e["mc_number"]=m.group(1)
    if m:=price_re.search(t): e["price"]=float(m.group(1))
    if m:=loadid_re.search(t): e["load_id"]=m.group(0)
    return e
def simple_sentiment(t:str)->str:
    t=t.lower(); pos=sum(t.count(w) for w in["good","ok","yes","great"]); neg=sum(t.count(w) for w in["no","bad","reject"])
    return "positive" if pos>neg else ("negative" if neg>pos else "neutral")

_fmcsa_cache={}; CACHE_TTL=86400
def _mock(mc): return {"mcNumber":mc,"legalName":f"Mock Carrier {mc}","allowToOperate":"Y","outOfService":"N","snapshotDate":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"source":"mock"}
def fmcs_lookup_by_mc(mc:str)->Dict[str,Any]:
    if FMCSA_MODE=="mock" or not FMCSA_WEBKEY: return _mock(mc)
    try:
        r=requests.get(f"https://mobile.fmcsa.dot.gov/qc/services/companySnapshot?webKey={FMCSA_WEBKEY}&mcNumber={mc}",timeout=8)
        r.raise_for_status(); d=r.json(); return d if isinstance(d,dict) else _mock(mc)
    except Exception: return _mock(mc)

@app.post("/api/authenticate",dependencies=[Depends(require_api_key)])
def auth(c:CarrierIn):
    metrics["calls_total"]+=1
    s=fmcs_lookup_by_mc(c.mc_number.strip())
    allowed=True
    return {"eligible":allowed,"carrier":s}

@app.get("/api/loads",response_model=List[LoadOut],dependencies=[Depends(require_api_key)])
def loads(): return load_loads()[:10]

@app.post("/api/negotiate",dependencies=[Depends(require_api_key)])
def neg(p:NegotiateIn):
    k=f"{p.mc_number}:{p.load_id}"
    loads=load_loads(); l=next((x for x in loads if x["load_id"]==p.load_id),None)
    if not l: raise HTTPException(404,"load not found")
    listed=float(l["loadboard_rate"]); ceil=round(listed*(1+MAX_OVER_PCT),2)
    st=negotiations.get(k,{"round":0,"settled":False})
    offer=parse_amount(p.offer)
    if offer<=ceil:
        st.update(settled=True,price=offer); negotiations[k]=st
        metrics["offers_accepted"]+=1; return {"accepted":True,"price":offer}
    if st["round"]>=3:
        metrics["offers_rejected"]+=1; return {"accepted":False,"reason":"max rounds"}
    st["round"]+=1; negotiations[k]=st
    return {"accepted":False,"counter_offer":ceil,"round":st["round"]}

@app.post("/api/call/result",dependencies=[Depends(require_api_key)])
def callres(p:CallResultIn):
    e=extract_entities_from_text(p.transcript or ""); s=simple_sentiment(p.transcript)
    call_results.append({"ts":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"mc_number":p.mc_number or e.get("mc_number"),
                         "load_id":p.load_id or e.get("load_id"),"final_price":p.final_price,"accepted":p.accepted,"sentiment":s,"entities":e})
    return {"ok":True}

def _filter(calls, f,t):
    return [r for r in calls if (not f or r["ts"][:10]>=f) and (not t or r["ts"][:10]<=t)]
def _agg(calls):
    d={}; 
    for r in calls:
        day=r["ts"][:10]; b=d.setdefault(day,{"accepted":0,"rejected":0})
        (b["accepted"] if r["accepted"] else b["rejected"]) += 1 if r.get("accepted") is not None else 0
    return [{"date":k,**v} for k,v in sorted(d.items())]

@app.get("/dashboard",response_class=HTMLResponse)
def dash():
    if not PUBLIC_DASHBOARD: raise HTTPException(403)
    html="""
<!doctype html><html><head><meta charset=utf-8><title>Dashboard</title>
<style>body{font-family:system-ui;margin:24px;color:#111}
.kpis{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}
.card{border:1px solid #ddd;border-radius:10px;padding:10px 14px;min-width:150px}
.label{font-size:12px;color:#666;text-transform:uppercase}
.value{font-size:22px;font-weight:600}
.quick button{background:#2563eb;color:#fff;border:none;padding:6px 10px;border-radius:8px;cursor:pointer}
canvas{border:1px solid #eee;border-radius:10px;max-width:100%;margin-top:10px}
</style></head><body>
<h1>Inbound Calls Metrics</h1>
<div class=controls>
<input id=from type=date><input id=to type=date>
<button id=apply>Apply filters</button>
<div class=quick>
<button id=q7>Last 7d</button><button id=q14>14d</button><button id=q30>30d</button>
<button id=qToday>Today</button><button id=qClear>Clear</button>
</div></div>
<div class=kpis>
<div class=card><div class=label>Calls</div><div id=calls class=value>–</div></div>
<div class=card><div class=label>Accepted</div><div id=acc class=value>–</div></div>
<div class=card><div class=label>Rejected</div><div id=rej class=value>–</div></div>
<div class=card><div class=label>Acceptance rate</div><div id=rate class=value>–</div></div>
</div>
<canvas id=chart width=900 height=320></canvas>
<script>
const F=document.getElementById('from'),T=document.getElementById('to'),C=document.getElementById('chart').getContext('2d');
function qs(){let p=new URLSearchParams();if(F.value)p.set('from',F.value);if(T.value)p.set('to',T.value);return p.toString()?'?'+p:'';}
function fmt(d){return new Date(d).toISOString().slice(0,10);}
function setR(days){let n=new Date(),to=new Date(Date.UTC(n.getUTCFullYear(),n.getUTCMonth(),n.getUTCDate())),f=new Date(to);f.setUTCDate(to.getUTCDate()-(days-1));F.value=fmt(f);T.value=fmt(to);load();}
function clearF(){F.value=T.value='';load();}
async function load(){const r=await fetch('/dashboard/data'+qs());if(!r.ok)return;const j=await r.json();document.getElementById('calls').textContent=j.calls_logged||0;
document.getElementById('acc').textContent=j.accepted_in_range||0;document.getElementById('rej').textContent=j.rejected_in_range||0;
const tot=j.calls_logged||0,a=j.accepted_in_range||0;document.getElementById('rate').textContent=tot?((a/tot*100).toFixed(1)+'%'):'–';draw(j.daily_counts||[]);}
function draw(rows){C.clearRect(0,0,900,320);let l=rows.map(r=>r.date),A=rows.map(r=>r.accepted||0),R=rows.map(r=>r.rejected||0),max=Math.max(1,...A,...R);
const padL=50,padB=40,h=280,w=800,g=20,bw=12;for(let i=0;i<l.length;i++){let x=padL+i*(bw*3),yA=h-(A[i]/max)*h,yR=h-(R[i]/max)*h;
C.fillStyle='#3b82f6';C.fillRect(x,yA,bw,(A[i]/max)*h);C.fillStyle='#ef4444';C.fillRect(x+bw+4,yR,bw,(R[i]/max)*h);
C.fillStyle='#555';C.fillText(l[i].slice(5),x,310);}}
document.getElementById('apply').onclick=load;
['q7','q14','q30'].forEach((id,i)=>document.getElementById(id).onclick=()=>setR([7,14,30][i]));
document.getElementById('qToday').onclick=()=>setR(1);
document.getElementById('qClear').onclick=clearF;
load();setInterval(load,5000);
</script></body></html>"""
    return HTMLResponse(html)

@app.get("/dashboard/data")
def data(from_date:Optional[str]=None,to_date:Optional[str]=None):
    if not PUBLIC_DASHBOARD: raise HTTPException(403)
    f,t=from_date,to_date
    fl=_filter(call_results,f,t); daily=_agg(fl)
    acc=sum(1 for x in fl if x.get("accepted") is True)
    rej=sum(1 for x in fl if x.get("accepted") is False)
    return {"calls_logged":len(fl),"accepted_in_range":acc,"rejected_in_range":rej,"daily_counts":daily}

@app.get("/")
def root(): return {"message":"Inbound Carrier Agent running - V12 (smooth dashboard)"}
