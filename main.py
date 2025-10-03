"""
main.py - PoC Inbound Carrier Agent API (FastAPI)

Endpoints:
- POST /api/authenticate        -> { mc_number }  (verifica FMCSA o mock)
- GET  /api/loads               -> lista de cargas del fichero data/loads.json
- POST /api/negotiate           -> { mc_number, load_id, offer } -> negociación (hasta 3 rondas)
- POST /api/call/result         -> { transcript, mc_number, load_id, final_price?, accepted? }
- GET  /api/metrics             -> JSON con métricas básicas del PoC

Notes:
- Cabecera requerida: x-api-key (configurar API_KEY en .env)
- Para FMCSA real: configurar FMCSA_WEBKEY en .env y ajustar endpoint si la doc oficial varía.
- Mock fallback: si no hay FMCSA_WEBKEY, devuelve respuesta simulada (útil en desarrollo).
"""

import os
import re
import json
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Header, Depends
from pydantic import BaseModel
import requests

# -------------------------
# Config
# -------------------------
API_KEY = os.getenv("API_KEY", "test-api-key")
FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY", "")  # si está vacío usamos mock
LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MIN_ACCEPT_PCT = float(os.getenv("MIN_ACCEPT_PCT", "0.85"))  # accept >= 85% del rate por defecto

# -------------------------
# App & in-memory stores (PoC)
# -------------------------
app = FastAPI(title="HappyRobot FDE - Inbound Carrier API PoC")

# negotiation state: key = f"{mc}:{load_id}" => { round:int, settled:bool, price:int, listed:float, history:[] }
negotiations: Dict[str, Dict[str, Any]] = {}

# store call results
call_results: List[Dict[str, Any]] = []

# metrics (simple counters)
metrics = {
    "calls_total": 0,
    "auth_failures": 0,
    "offers_accepted": 0,
    "offers_rejected": 0,
    "negotiation_rounds_total": 0,
}

# -------------------------
# Models
# -------------------------
class CarrierIn(BaseModel):
    mc_number: str

class LoadOut(BaseModel):
    load_id: str
    origin: str
    destination: str
    pickup_datetime: str
    delivery_datetime: str
    equipment_type: str
    loadboard_rate: float
    notes: Optional[str] = None
    weight: Optional[float] = None
    commodity_type: Optional[str] = None
    num_of_pieces: Optional[int] = None
    miles: Optional[float] = None
    dimensions: Optional[str] = None

class NegotiateIn(BaseModel):
    mc_number: str
    load_id: str
    offer: float

class CallResultIn(BaseModel):
    transcript: str
    mc_number: Optional[str] = None
    load_id: Optional[str] = None
    final_price: Optional[float] = None
    accepted: Optional[bool] = None

# -------------------------
# Helpers: API key middleware
# -------------------------
def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")

# -------------------------
# Helper: load loads.json
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# -------------------------
# Helper: FMCSA lookup (mock fallback)
# -------------------------
_fmcsa_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 3600

def fmcs_lookup_by_mc(mc_number: str) -> Dict[str, Any]:
    mc = mc_number.strip()
    # cache simple
    entry = _fmcsa_cache.get(mc)
    if entry and (time.time() - entry["ts"] < CACHE_TTL_SECONDS):
        return entry["data"]

    if not FMCSA_WEBKEY:
        # MOCK response for development
        mock = {
            "mcNumber": mc,
            "legalName": f"Mock Carrier {mc}",
            "allowToOperate": "Y",
            "outOfService": "N",
            "snapshotDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _fmcsa_cache[mc] = {"ts": time.time(), "data": mock}
        return mock

    # If FMCSA_WEBKEY is set, try real lookup. Adjust URL to FMCSA docs if different.
    try:
        # NOTE: the path/query may differ depending on FMCSA doc. Adjust as necessary.
        base = "https://mobile.fmcsa.dot.gov/qc/services/"
        # companySnapshot endpoint typically used; confirm with FMCSA docs.
        url = f"{base}companySnapshot?webKey={FMCSA_WEBKEY}&mcNumber={mc}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        # normalise minimal fields
        _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
        return data
    except Exception as e:
        # Bubble up for caller to decide; here we return a structured error-like object
        raise RuntimeError(f"FMCSA lookup failed: {str(e)}")

# -------------------------
# Helper: simple extraction & sentiment
# -------------------------
price_re = re.compile(r"\b(?:\$)?\s*(\d{2,6}(?:\.\d{1,2})?)\b")
mc_re = re.compile(r"\bMC(?:\s|#|:)?\s*(\d{4,10})\b", re.IGNORECASE)
loadid_re = re.compile(r"\bL\d{3,}\b", re.IGNORECASE)

def extract_entities_from_text(text: str) -> Dict[str, Optional[str]]:
    text = text or ""
    entities = {}
    m_mc = mc_re.search(text)
    if m_mc:
        entities["mc_number"] = m_mc.group(1)
    m_price = price_re.search(text.replace(",", ""))
    if m_price:
        entities["price"] = float(m_price.group(1))
    m_load = loadid_re.search(text)
    if m_load:
        entities["load_id"] = m_load.group(0)
    return entities

def simple_sentiment(text: str) -> str:
    if not text:
        return "neutral"
    t = text.lower()
    positive_tokens = ["good", "great", "ok", "thanks", "thank", "yes", "happy", "accept"]
    negative_tokens = ["no", "not", "reject", "angry", "bad", "hate", "problem"]
    pos = sum(t.count(tok) for tok in positive_tokens)
    neg = sum(t.count(tok) for tok in negative_tokens)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"

# -------------------------
# Helper: negotiation policy
# -------------------------
def compute_counter(listed: float, offer: float, round_idx: int) -> float:
    # initial counter: midpoint; subsequent counters concede less
    if round_idx == 0:
        return round((listed + offer) / 2, 2)
    # concession factor decreases each round
    concession = (listed - offer) * (0.5 * (0.7 ** (round_idx - 1)))
    counter = round(listed - concession, 2)
    return counter

# -------------------------
# Routes
# -------------------------
@app.post("/api/authenticate", dependencies=[Depends(require_api_key)])
def authenticate(carrier: CarrierIn):
    """
    Verify carrier via FMCSA or return mock if no FMCSA_WEBKEY.
    """
    metrics["calls_total"] += 1
    mc = carrier.mc_number.strip()
    try:
        snapshot = fmcs_lookup_by_mc(mc)
    except Exception as e:
        metrics["auth_failures"] += 1
        raise HTTPException(status_code=502, detail=f"FMCSA lookup failed: {str(e)}")
    # Basic eligibility logic
    allowed = False
    if isinstance(snapshot, dict):
        allow = snapshot.get("allowToOperate") or snapshot.get("allow_to_operate") or snapshot.get("allow")
        out = snapshot.get("outOfService") or snapshot.get("out_of_service")
        if allow in ("Y", "Yes", True, "yes", "y") and out not in ("Y", "Yes", True):
            allowed = True
        else:
            # if it's a mock response that uses other fields, be permissive
            if not FMCSA_WEBKEY:
                allowed = True
    return {"eligible": allowed, "carrier": snapshot}

@app.get("/api/loads", response_model=List[LoadOut], dependencies=[Depends(require_api_key)])
def get_loads(origin: Optional[str] = None, destination: Optional[str] = None, max_miles: Optional[float] = None):
    loads = load_loads()
    def match(l):
        if origin and origin.lower() not in l.get("origin","").lower():
            return False
        if destination and destination.lower() not in l.get("destination","").lower():
            return False
        if max_miles and l.get("miles") and float(l.get("miles")) > float(max_miles):
            return False
        return True
    filtered = [l for l in loads if match(l)]
    return filtered[:10]

@app.post("/api/negotiate", dependencies=[Depends(require_api_key)])
def negotiate(payload: NegotiateIn):
    """
    Negotiate price:
      - Accept if offer >= MIN_ACCEPT_PCT * listed_rate
      - Otherwise return counter_offer
      - Max 3 rounds
    """
    key = f"{payload.mc_number}:{payload.load_id}"
    loads = load_loads()
    load = next((l for l in loads if l.get("load_id") == payload.load_id), None)
    if not load:
        raise HTTPException(status_code=404, detail="load not found")
    listed = float(load.get("loadboard_rate", 0))
    state = negotiations.get(key, {"round": 0, "settled": False, "listed": listed, "history": []})
    if state["settled"]:
        return {"accepted": True, "price": state.get("price"), "rounds": state["round"], "note": "already settled"}

    offer = float(payload.offer)
    state["history"].append({"type":"offer","value":offer,"ts":time.time()})
    # accept threshold
    min_accept = round(listed * MIN_ACCEPT_PCT, 2)
    if offer >= min_accept:
        state["settled"] = True
        state["price"] = offer
        negotiations[key] = state
        metrics["offers_accepted"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        return {"accepted": True, "price": offer, "round": state["round"]}
    # if reached max rounds
    if state["round"] >= 3:
        metrics["offers_rejected"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        state["settled"] = False
        negotiations[key] = state
        return {"accepted": False, "reason": "max rounds reached", "round": state["round"]}

    counter = compute_counter(listed, offer, state["round"])
    state["round"] += 1
    state["history"].append({"type":"counter","value":counter,"ts":time.time()})
    negotiations[key] = state
    return {"accepted": False, "counter_offer": counter, "round": state["round"]}

@app.post("/api/call/result", dependencies=[Depends(require_api_key)])
def call_result(payload: CallResultIn):
    """
    Receive final call transcript & metadata. Extract entities, sentiment, save summary.
    """
    ent = extract_entities_from_text(payload.transcript or "")
    sentiment = simple_sentiment(payload.transcript)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mc_number": payload.mc_number or ent.get("mc_number"),
        "load_id": payload.load_id or ent.get("load_id"),
        "transcript": payload.transcript,
        "entities": ent,
        "final_price": payload.final_price,
        "accepted": payload.accepted,
        "sentiment": sentiment
    }
    call_results.append(record)
    return {"ok": True, "summary": record}

@app.get("/api/metrics", dependencies=[Depends(require_api_key)])
def get_metrics():
    """
    Return simple PoC metrics as JSON. In production, export Prometheus metrics.
    """
    # compute avg negotiation rounds (simple)
    avg_rounds = None
    if metrics["offers_accepted"] + metrics["offers_rejected"] > 0:
        avg_rounds = metrics["negotiation_rounds_total"] / max(1, (metrics["offers_accepted"] + metrics["offers_rejected"]))
    return {
        "metrics": metrics,
        "avg_negotiation_rounds": avg_rounds,
        "calls_logged": len(call_results),
        "recent_calls": call_results[-10:]
    }

# Root & readiness
@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent PoC running - V3"}

# -------------------------
# Notes for next steps (not code):
# - Replace mock FMCSA behavior by configuring FMCSA_WEBKEY and ensuring the FMCSA endpoint matches docs.
# - Persist negotiations and call_results to a DB (SQLite/Postgres) in prod.
# - Improve NLU (use Whisper -> STT -> transformer NER + sentiment).
# - Add TLS + API gateway and rate-limiting.
# -------------------------


