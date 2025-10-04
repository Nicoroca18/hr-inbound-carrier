"""
main.py - PoC Inbound Carrier Agent API (FastAPI)

Endpoints:
- POST /api/authenticate        -> { mc_number }  (verifica FMCSA o mock)
- GET  /api/loads               -> lista de cargas del fichero data/loads.json
- POST /api/negotiate           -> { mc_number, load_id, offer } -> negociación (hasta 3 rondas)
- POST /api/call/result         -> { transcript, mc_number, load_id, final_price?, accepted? }
- GET  /api/metrics             -> JSON con métricas básicas del PoC

Notas:
- Cabecera requerida: x-api-key (configurar API_KEY en env)
- Para FMCSA real: configurar FMCSA_WEBKEY en env.
- Mock fallback: si no hay FMCSA_WEBKEY, devuelve simulación (útil en desarrollo).
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
# Config (por entorno; nunca hardcodear secretos)
# -------------------------
API_KEY = os.getenv("API_KEY")  # si no está, devolvemos 500 en require_api_key
FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY")  # si no está, usamos MOCK
LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MIN_ACCEPT_PCT = float(os.getenv("MIN_ACCEPT_PCT", "0.85"))  # aceptar >= 85% del rate

# -------------------------
# App & in-memory stores (PoC)
# -------------------------
app = FastAPI(title="HappyRobot FDE - Inbound Carrier API PoC")

# negotiation state: key = f"{mc}:{load_id}" => { round:int, settled:bool, price:float, listed:float, history:list }
negotiations: Dict[str, Dict[str, Any]] = {}

# store call results (PoC)
call_results: List[Dict[str, Any]] = []

# metrics (contadores simples)
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
# Seguridad: API key (header x-api-key)
# -------------------------
def require_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    # Si el servidor no tiene API_KEY configurada, es una mala configuración
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server misconfigured: API_KEY not set")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")
    return True

# -------------------------
# Utilidades
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# -------- FMCSA lookup (mock fallback + normalización de respuesta) --------
_fmcsa_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 3600

def _normalize_fmcsa_snapshot(raw: Dict[str, Any], mc: str) -> Dict[str, Any]:
    """
    Intenta normalizar distintos formatos posibles de la API FMCSA a un shape común:
    {
      "mcNumber": str,
      "legalName": str|None,
      "allowToOperate": "Y"/"N",
      "outOfService": "Y"/"N",
      "snapshotDate": ISO8601|None,
      "source": "FMCSA"|"mock"
    }
    """
    # paths típicos: algunos devuelven content/[], otros companySnapshot, etc.
    def get_any(d: Any, keys: List[str]):
        for k in keys:
            if isinstance(d, dict) and k in d:
                return d[k]
        return None

    # si viene anidado, intenta descender
    candidate = raw
    # algunos retornan {"content":[{...}]} → toma primer elemento
    if isinstance(candidate, dict) and "content" in candidate and isinstance(candidate["content"], list) and candidate["content"]:
        candidate = candidate["content"][0]

    mc_number = str(get_any(candidate, ["mcNumber", "mc", "MC"])) if isinstance(candidate, dict) else None
    legal_name = get_any(candidate, ["legalName", "legal_name", "dbaName", "name"])
    allow = get_any(candidate, ["allowToOperate", "allow_to_operate", "authorizedForProperty"])
    outsvc = get_any(candidate, ["outOfService", "out_of_service", "outOfServiceDate"])
    snap = get_any(candidate, ["snapshotDate", "snapshot_date", "lastUpdated"])

    # Normaliza flags a "Y"/"N" en lo posible
    def to_YN(v, default="N"):
        if v is None:
            return default
        if isinstance(v, bool):
            return "Y" if v else "N"
        s = str(v).strip().lower()
        if s in ("y", "yes", "true", "t", "1", "authorized", "active"):
            return "Y"
        return "N"

    normalized = {
        "mcNumber": mc_number or mc,
        "legalName": legal_name,
        "allowToOperate": to_YN(allow, default="Y" if not FMCSA_WEBKEY else "N"),
        "outOfService": to_YN(False if outsvc in (None, "", "None") else True) if isinstance(outsvc, str) and outsvc not in ("", "None") else to_YN("N"),
        "snapshotDate": snap,
        "source": "FMCSA" if FMCSA_WEBKEY else "mock",
        "raw": raw,  # opcional: útil para debug
    }
    return normalized

def fmcs_lookup_by_mc(mc_number: str) -> Dict[str, Any]:
    mc = mc_number.strip()
    # cache simple
    entry = _fmcsa_cache.get(mc)
    if entry and (time.time() - entry["ts"] < CACHE_TTL_SECONDS):
        return entry["data"]

    if not FMCSA_WEBKEY:
        # MOCK response para desarrollo/demo
        mock = {
            "mcNumber": mc,
            "legalName": f"Mock Carrier {mc}",
            "allowToOperate": "Y",
            "outOfService": "N",
            "snapshotDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "mock",
        }
        _fmcsa_cache[mc] = {"ts": time.time(), "data": mock}
        return mock

    # Lookup real: mantén este endpoint si te funciona; si la doc cambia, ajusta query/parseo
    try:
        base = "https://mobile.fmcsa.dot.gov/qc/services"
        url = f"{base}/companySnapshot"
        params = {"webKey": FMCSA_WEBKEY, "mcNumber": mc}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        normalized = _normalize_fmcsa_snapshot(data, mc)
        _fmcsa_cache[mc] = {"ts": time.time(), "data": normalized}
        return normalized
    except Exception as e:
        raise RuntimeError(f"FMCSA lookup failed: {e}")

# -------- Extracción simple & sentimiento (PoC) --------
price_re = re.compile(r"\b(?:\$)?\s*(\d{2,6}(?:\.\d{1,2})?)\b")
mc_re = re.compile(r"\bMC(?:\s|#|:)?\s*(\d{4,10})\b", re.IGNORECASE)
loadid_re = re.compile(r"\bL\d{3,}\b", re.IGNORECASE)

def extract_entities_from_text(text: str) -> Dict[str, Optional[str]]:
    text = text or ""
    entities: Dict[str, Optional[str]] = {}
    m_mc = mc_re.search(text)
    if m_mc:
        entities["mc_number"] = m_mc.group(1)
    m_price = price_re.search(text.replace(",", ""))
    if m_price:
        try:
            entities["price"] = float(m_price.group(1))
        except ValueError:
            entities["price"] = None
    m_load = loadid_re.search(text)
    if m_load:
        entities["load_id"] = m_load.group(0).upper()
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

# -------- Política de negociación --------
def compute_counter(listed: float, offer: float, round_idx: int) -> float:
    # Ronda 0: punto medio; luego concesión decreciente
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
    Verifica carrier via FMCSA (real si hay FMCSA_WEBKEY, mock si no).
    """
    metrics["calls_total"] += 1
    mc = carrier.mc_number.strip()
    try:
        snapshot = fmcs_lookup_by_mc(mc)
    except Exception as e:
        metrics["auth_failures"] += 1
        raise HTTPException(status_code=502, detail=f"FMCSA lookup failed: {e}")

    # Elegibilidad básica con los campos normalizados
    allow = str(snapshot.get("allowToOperate", "N")).upper()
    outsvc = str(snapshot.get("outOfService", "N")).upper()
    allowed = (allow == "Y") and (outsvc != "Y")

    # En mock, sé permisivo si falta algo
    if snapshot.get("source") == "mock":
        allowed = True

    return {"eligible": allowed, "carrier": snapshot}

@app.get("/api/loads", response_model=List[LoadOut], dependencies=[Depends(require_api_key)])
def get_loads(origin: Optional[str] = None, destination: Optional[str] = None, max_miles: Optional[float] = None):
    loads = load_loads()

    def match(l):
        if origin and origin.lower() not in str(l.get("origin", "")).lower():
            return False
        if destination and destination.lower() not in str(l.get("destination", "")).lower():
            return False
        if max_miles is not None and l.get("miles") is not None:
            try:
                if float(l.get("miles")) > float(max_miles):
                    return False
            except Exception:
                pass
        return True

    filtered = [l for l in loads if match(l)]
    return filtered[:10]

@app.post("/api/negotiate", dependencies=[Depends(require_api_key)])
def negotiate(payload: NegotiateIn):
    """
    Negociación:
      - Acepta si offer >= MIN_ACCEPT_PCT * listed_rate
      - Si no, devuelve counter_offer
      - Máximo 3 rondas (round 0..3 -> al llegar a 3 sin aceptar, se corta)
    """
    key = f"{payload.mc_number.strip()}:{payload.load_id.strip()}"
    loads = load_loads()
    load = next(
        (l for l in loads if str(l.get("load_id")).strip().upper() == str(payload.load_id).strip().upper()),
        None
    )
    if not load:
        raise HTTPException(status_code=404, detail="load not found")

    listed = float(load.get("loadboard_rate", 0.0))
    state = negotiations.get(key, {"round": 0, "settled": False, "listed": listed, "history": []})

    if state["settled"]:
        return {"accepted": True, "price": state.get("price"), "round": state["round"], "note": "already settled"}

    offer = float(payload.offer)
    state["history"].append({"type": "offer", "value": offer, "ts": time.time()})

    min_accept = round(listed * MIN_ACCEPT_PCT, 2)
    if offer >= min_accept:
        state["settled"] = True
        state["price"] = offer
        negotiations[key] = state
        metrics["offers_accepted"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        return {"accepted": True, "price": offer, "round": state["round"]}

    # Si ya estamos en 3 rondas sin aceptar, cortar
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
    Recibe transcript & metadatos finales. Extrae entidades y sentimiento y guarda un resumen.
    """
    ent = extract_entities_from_text(payload.transcript or "")
    sentiment = simple_sentiment(payload.transcript)

    # Tolerar final_price como string numérica (si la plataforma la envía así)
    final_price_val: Optional[float] = payload.final_price
    if final_price_val is None and ent.get("price") is not None:
        try:
            final_price_val = float(ent["price"])
        except Exception:
            final_price_val = None

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mc_number": (payload.mc_number or ent.get("mc_number")),
        "load_id": (payload.load_id or ent.get("load_id")),
        "transcript": payload.transcript,
        "entities": ent,
        "final_price": final_price_val,
        "accepted": payload.accepted,
        "sentiment": sentiment,
    }
    call_results.append(record)
    return {"ok": True, "summary": record}

@app.get("/api/metrics", dependencies=[Depends(require_api_key)])
def get_metrics():
    """
    Devuelve métricas simples de PoC.
    """
    total_outcomes = metrics["offers_accepted"] + metrics["offers_rejected"]
    avg_rounds = (metrics["negotiation_rounds_total"] / total_outcomes) if total_outcomes > 0 else None
    return {
        "metrics": metrics,
        "avg_negotiation_rounds": avg_rounds,
        "calls_logged": len(call_results),
        "recent_calls": call_results[-10:]
    }

# Root & readiness
@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent PoC running - V4"}


