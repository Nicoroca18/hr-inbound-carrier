"""
main.py - Inbound Carrier Agent API (FastAPI)

Endpoints:
- POST /api/authenticate  -> { mc_number }           (FMCSA real o fallback mock según FMCSA_MODE)
- GET  /api/loads         -> lista de cargas (data/loads.json)
- POST /api/negotiate     -> { mc_number, load_id, offer } (hasta 3 rondas)
- POST /api/call/result   -> { transcript, mc_number?, load_id?, final_price?, accepted? }
- GET  /api/metrics       -> métricas simples (PoC)

Notas clave para HappyRobot:
- Acepta "offer" como número o string (ej. "1200", "$1,200", "twelve hundred" si la UI lo convierte a texto).
- "Preserves data types" en tus webhooks puede ir ON u OFF; el backend soporta ambas.
"""

import os
import re
import json
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
import requests

# -------------------------
# Config
# -------------------------
API_KEY = os.getenv("API_KEY", "test-api-key")

FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY", "")
FMCSA_MODE = os.getenv("FMCSA_MODE", "real").lower()  # 'real' | 'auto' | 'mock'

LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MIN_ACCEPT_PCT = float(os.getenv("MIN_ACCEPT_PCT", "0.85"))  # acepta >= 85% del rate por defecto

# -------------------------
# App & memoria (PoC)
# -------------------------
app = FastAPI(title="HappyRobot - Inbound Carrier API")

# estado de negociaciones en memoria
# key = f"{mc}:{load_id}" => { round:int, settled:bool, price:float, listed:float, history:list }
negotiations: Dict[str, Dict[str, Any]] = {}

# resultados de llamadas
call_results: List[Dict[str, Any]] = []

# métricas simples
metrics = {
    "calls_total": 0,
    "auth_failures": 0,
    "offers_accepted": 0,
    "offers_rejected": 0,
    "negotiation_rounds_total": 0,
    "fmcsa_real_calls": 0,
    "fmcsa_real_errors": 0,
    "fmcsa_mock_uses": 0,
}

# -------------------------
# Modelos
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
    # HappyRobot a veces envía strings: soportamos Any y lo convertimos robustamente
    offer: Any

class CallResultIn(BaseModel):
    transcript: str
    mc_number: Optional[str] = None
    load_id: Optional[str] = None
    # también puede llegar como string
    final_price: Optional[Any] = None
    accepted: Optional[bool] = None

# -------------------------
# Auth middleware
# -------------------------
def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")

# -------------------------
# Helpers comunes
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# parsing robusto de importes
_num_re = re.compile(r"(-?\d{1,7}(?:\.\d{1,2})?)")

def parse_amount(value: Any) -> float:
    """
    Convierte a float de forma robusta:
    - int/float => float
    - str => quita $, comas, espacios y toma el primer número
    - si no hay dígitos válidos => 422
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        m = _num_re.search(s)
        if m:
            try:
                return float(m.group(1))
            except:
                pass
    raise HTTPException(status_code=422, detail="Invalid offer: must be a numeric amount")

# extracción simple (para /api/call/result)
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
    negative_tokens = ["no", "not", "reject", "angry", "bad", "hate", "problem", "can't", "cannot"]
    pos = sum(t.count(tok) for tok in positive_tokens)
    neg = sum(t.count(tok) for tok in negative_tokens)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"

# FMCSA lookup (con cache + modo 'auto' resiliente)
_fmcsa_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 3600

def _mock_snapshot(mc: str) -> Dict[str, Any]:
    return {
        "mcNumber": mc,
        "legalName": f"Mock Carrier {mc}",
        "allowToOperate": "Y",
        "outOfService": "N",
        "snapshotDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "mock",
        "degraded": (FMCSA_MODE == "auto"),
    }

def fmcs_lookup_by_mc(mc_number: str) -> Dict[str, Any]:
    mc = mc_number.strip()
    # cache básico
    entry = _fmcsa_cache.get(mc)
    if entry and (time.time() - entry["ts"] < CACHE_TTL_SECONDS):
        return entry["data"]

    # Modo MOCK directo
    if FMCSA_MODE == "mock" or not FMCSA_WEBKEY:
        data = _mock_snapshot(mc)
        _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
        if FMCSA_MODE != "mock":
            metrics["fmcsa_mock_uses"] += 1
        return data

    # Modo REAL (estricto): si falla, levantamos error
    if FMCSA_MODE == "real":
        try:
            metrics["fmcsa_real_calls"] += 1
            base = "https://mobile.fmcsa.dot.gov/qc/services/"
            # companySnapshot admite mcNumber o usdot:
            url = f"{base}companySnapshot?webKey={FMCSA_WEBKEY}&mcNumber={mc}"
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            data = r.json()
            # normalizamos algunos campos
            if isinstance(data, dict):
                data.setdefault("source", "FMCSA")
                data.setdefault("degraded", False)
            _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
            return data
        except Exception as e:
            metrics["fmcsa_real_errors"] += 1
            raise RuntimeError(f"FMCSA lookup failed: {str(e)}")

    # Modo AUTO: intenta real, si falla, mock (degradado)
    if FMCSA_MODE == "auto":
        try:
            metrics["fmcsa_real_calls"] += 1
            base = "https://mobile.fmcsa.dot.gov/qc/services/"
            url = f"{base}companySnapshot?webKey={FMCSA_WEBKEY}&mcNumber={mc}"
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                data.setdefault("source", "FMCSA")
                data.setdefault("degraded", False)
            _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
            return data
        except Exception as e:
            metrics["fmcsa_real_errors"] += 1
            # fallback silencioso a mock
            data = _mock_snapshot(mc)
            metrics["fmcsa_mock_uses"] += 1
            _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
            return data

    # si llega aquí, tratamos como mock
    data = _mock_snapshot(mc)
    _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
    return data

# negociación: política de contraoferta
def compute_counter(listed: float, offer: float, round_idx: int) -> float:
    # 1ª ronda: punto medio; siguientes, concesión decreciente
    if round_idx == 0:
        return round((listed + offer) / 2, 2)
    concession = (listed - offer) * (0.5 * (0.7 ** (round_idx - 1)))
    counter = round(listed - concession, 2)
    return counter

# -------------------------
# Rutas
# -------------------------
@app.post("/api/authenticate", dependencies=[Depends(require_api_key)])
def authenticate(carrier: CarrierIn):
    """
    Verifica el MC en FMCSA (real / auto / mock).
    """
    metrics["calls_total"] += 1
    mc = carrier.mc_number.strip()
    try:
        snapshot = fmcs_lookup_by_mc(mc)
    except Exception as e:
        metrics["auth_failures"] += 1
        raise HTTPException(status_code=502, detail=f"FMCSA lookup failed: {str(e)}")

    # Lógica de elegibilidad básica
    allowed = False
    if isinstance(snapshot, dict):
        allow = snapshot.get("allowToOperate") or snapshot.get("allow_to_operate") or snapshot.get("allow")
        out = snapshot.get("outOfService") or snapshot.get("out_of_service")
        if allow in ("Y", "Yes", True, "yes", "y") and out not in ("Y", "Yes", True, "yes", "y"):
            allowed = True
        else:
            # en mock permitimos continuidad
            if snapshot.get("source") == "mock":
                allowed = True

    return {"eligible": allowed, "carrier": snapshot}

@app.get("/api/loads", response_model=List[LoadOut], dependencies=[Depends(require_api_key)])
def get_loads(origin: Optional[str] = None, destination: Optional[str] = None, max_miles: Optional[float] = None):
    loads = load_loads()
    def match(l):
        if origin and origin.lower() not in l.get("origin", "").lower():
            return False
        if destination and destination.lower() not in l.get("destination", "").lower():
            return False
        if max_miles and l.get("miles") and float(l.get("miles")) > float(max_miles):
            return False
        return True
    filtered = [l for l in loads if match(l)]
    return filtered[:10]

@app.post("/api/negotiate", dependencies=[Depends(require_api_key)])
def negotiate(payload: NegotiateIn):
    """
    Negociación:
      - Acepta si offer >= MIN_ACCEPT_PCT * rate listado
      - Si no, devuelve counter_offer
      - Máximo 3 rondas (0,1,2 -> counters; si >=3 => fin)
    """
    key = f"{payload.mc_number}:{payload.load_id}"

    # localizar el load (normalizando)
    loads = load_loads()
    load = next((l for l in loads if str(l.get("load_id")).strip() == str(payload.load_id).strip()), None)
    if not load:
        raise HTTPException(status_code=404, detail="load not found")

    listed = float(load.get("loadboard_rate", 0))
    state = negotiations.get(key, {"round": 0, "settled": False, "listed": listed, "history": []})
    if state["settled"]:
        return {"accepted": True, "price": state.get("price"), "rounds": state["round"], "note": "already settled"}

    offer = parse_amount(payload.offer)  # <— robusto (str/num)
    state["history"].append({"type": "offer", "value": offer, "ts": time.time()})

    min_accept = round(listed * MIN_ACCEPT_PCT, 2)
    if offer >= min_accept:
        state["settled"] = True
        state["price"] = offer
        negotiations[key] = state
        metrics["offers_accepted"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        return {"accepted": True, "price": offer, "round": state["round"]}

    if state["round"] >= 3:
        metrics["offers_rejected"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        state["settled"] = False
        negotiations[key] = state
        return {"accepted": False, "reason": "max rounds reached", "round": state["round"]}

    counter = compute_counter(listed, offer, state["round"])
    state["round"] += 1
    state["history"].append({"type": "counter", "value": counter, "ts": time.time()})
    negotiations[key] = state
    return {"accepted": False, "counter_offer": counter, "round": state["round"]}

@app.post("/api/call/result", dependencies=[Depends(require_api_key)])
def call_result(payload: CallResultIn):
    """
    Guarda resumen final. Si final_price llega como string, lo intentamos parsear.
    """
    ent = extract_entities_from_text(payload.transcript or "")
    sentiment = simple_sentiment(payload.transcript)

    final_price_val: Optional[float] = None
    if payload.final_price is not None:
        try:
            final_price_val = parse_amount(payload.final_price)
        except HTTPException:
            # si no parsea, lo dejamos en None; no bloqueamos el log
            final_price_val = None

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mc_number": (payload.mc_number or ent.get("mc_number")),
        "load_id": (payload.load_id or ent.get("load_id")),
        "transcript": payload.transcript,
        "entities": ent,
        "final_price": final_price_val,
        "accepted": payload.accepted,
        "sentiment": sentiment
    }
    call_results.append(record)
    return {"ok": True, "summary": record}

@app.get("/api/metrics", dependencies=[Depends(require_api_key)])
def get_metrics():
    avg_rounds = None
    total = metrics["offers_accepted"] + metrics["offers_rejected"]
    if total > 0:
        avg_rounds = metrics["negotiation_rounds_total"] / total
    return {
        "metrics": metrics,
        "avg_negotiation_rounds": avg_rounds,
        "calls_logged": len(call_results),
        "recent_calls": call_results[-10:]
    }

@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent running - V6"}




