"""
main.py - Inbound Carrier Agent API (FastAPI) - V5 (production-ready FMCSA)

Endpoints:
- POST /api/authenticate        -> { mc_number }  (verifica FMCSA real; mock opcional según modo)
- GET  /api/loads               -> lista de cargas del fichero data/loads.json
- POST /api/negotiate           -> { mc_number, load_id, offer } -> negociación (hasta 3 rondas)
- POST /api/call/result         -> { transcript, mc_number, load_id, final_price?, accepted? }
- GET  /api/metrics             -> métricas simples del PoC

Seguridad:
- Cabecera requerida: x-api-key (API_KEY por entorno)

FMCSA:
- FMCSA_WEBKEY obligatoria en modo "real".
- FMCSA_MODE: "real" (default) | "auto" | "mock"
  * real: solo llamadas reales; errores -> 502
  * auto: intenta real; si 401/403/5xx/timeout -> MOCK (marcado degraded=True)
  * mock: solo mock (dev)
"""

import os
import re
import json
import time
from typing import Optional, List, Dict, Any, Tuple

import requests
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

# -------------------------
# Config por entorno (NO hardcodear secretos)
# -------------------------
API_KEY = os.getenv("API_KEY")
FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY")  # requerida en modo real/auto para intentar real
FMCSA_MODE = os.getenv("FMCSA_MODE", "real").strip().lower()  # real | auto | mock
LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MIN_ACCEPT_PCT = float(os.getenv("MIN_ACCEPT_PCT", "0.85"))

# -------------------------
# App & mem stores
# -------------------------
app = FastAPI(title="HappyRobot FDE - Inbound Carrier API")

negotiations: Dict[str, Dict[str, Any]] = {}
call_results: List[Dict[str, Any]] = []
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
    offer: float

class CallResultIn(BaseModel):
    transcript: str
    mc_number: Optional[str] = None
    load_id: Optional[str] = None
    final_price: Optional[float] = None
    accepted: Optional[bool] = None

# -------------------------
# Seguridad: API key
# -------------------------
def require_api_key(x_api_key: str = Header(..., alias="x-api-key")):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="Server misconfigured: API_KEY not set")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")
    return True

# -------------------------
# Utilidades varias
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# -------- FMCSA client robusto --------
_fmcsa_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 3600

def _looks_like_usdot(s: str) -> bool:
    # heurística simple: USDOT o número de 5-8 dígitos muy típico
    s2 = s.strip().upper().replace("USDOT", "").strip()
    return s.upper().startswith("USDOT") or s2.isdigit()

def _normalize_flags_to_YN(v, default="N") -> str:
    if v is None:
        return default
    if isinstance(v, bool):
        return "Y" if v else "N"
    s = str(v).strip().lower()
    if s in ("y", "yes", "true", "t", "1", "authorized", "active"):
        return "Y"
    return "N"

def _pick_first_content(raw: Dict[str, Any]) -> Dict[str, Any]:
    # algunos endpoints devuelven {"content": [ ... ]}
    if isinstance(raw, dict) and "content" in raw and isinstance(raw["content"], list) and raw["content"]:
        return raw["content"][0]
    return raw

def _normalize_fmcsa_snapshot(raw: Dict[str, Any], identifier: str, source_tag: str) -> Dict[str, Any]:
    """
    Normaliza a shape común:
    {
      "mcNumber": str|None,
      "usdotNumber": str|None,
      "legalName": str|None,
      "allowToOperate": "Y"/"N",
      "outOfService": "Y"/"N",
      "snapshotDate": str|None,
      "source": source_tag ("FMCSA"|"mock"),
      "degraded": bool   # True si venimos de mock en modo auto
    }
    """
    cand = _pick_first_content(raw)
    def g(keys: List[str]):
        if isinstance(cand, dict):
            for k in keys:
                if k in cand:
                    return cand[k]
        return None

    mc = g(["mcNumber", "mc", "MC"])
    usdot = g(["usdotNumber", "usdot", "USDOT"])
    legal = g(["legalName", "legal_name", "dbaName", "name"])
    allow = g(["allowToOperate", "allow_to_operate", "authorizedForProperty"])
    outsvc = g(["outOfService", "out_of_service", "outOfServiceIndicator"])
    snap = g(["snapshotDate", "snapshot_date", "lastUpdated", "last_update_date"])

    normalized = {
        "mcNumber": str(mc) if mc else (identifier if not _looks_like_usdot(identifier) else None),
        "usdotNumber": str(usdot) if usdot else (identifier.replace("USDOT","").strip() if _looks_like_usdot(identifier) else None),
        "legalName": legal,
        "allowToOperate": _normalize_flags_to_YN(allow, default="N"),
        "outOfService": _normalize_flags_to_YN(outsvc, default="N"),
        "snapshotDate": snap,
        "source": source_tag,
        "degraded": False,
        "raw": raw,  # opcional para debugging
    }
    return normalized

def _http_get_json(url: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def _try_fmcsa_chain(identifier: str) -> Tuple[Dict[str, Any], str]:
    """
    Intenta varios endpoints FMCSA en cadena.
    Retorna (json, "endpoint_name") o levanta excepción en último fallo.
    """
    base = "https://mobile.fmcsa.dot.gov/qc/services"
    chain = []
    if _looks_like_usdot(identifier):
        # USDOT primero
        usd = identifier.replace("USDOT", "").strip()
        chain = [
            (f"{base}/carriers", {"webKey": FMCSA_WEBKEY, "dot": usd}, "carriers?dot"),
            (f"{base}/companySnapshot", {"webKey": FMCSA_WEBKEY, "usdot": usd}, "companySnapshot?usdot"),
        ]
    else:
        # MC primero
        mc = identifier.strip()
        chain = [
            (f"{base}/carriers", {"webKey": FMCSA_WEBKEY, "mc": mc}, "carriers?mc"),
            (f"{base}/companySnapshot", {"webKey": FMCSA_WEBKEY, "mcNumber": mc}, "companySnapshot?mcNumber"),
        ]
    last_exc = None
    for url, params, tag in chain:
        try:
            data = _http_get_json(url, params)
            return data, tag
        except Exception as e:
            last_exc = e
    raise RuntimeError(f"FMCSA chain failed: {last_exc}")

def _mock_snapshot(identifier: str) -> Dict[str, Any]:
    return {
        "mcNumber": None if _looks_like_usdot(identifier) else identifier.strip(),
        "usdotNumber": identifier.replace("USDOT", "").strip() if _looks_like_usdot(identifier) else None,
        "legalName": f"Mock Carrier {identifier.strip()}",
        "allowToOperate": "Y",
        "outOfService": "N",
        "snapshotDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "mock",
        "degraded": True,  # en modo auto lo marcamos como degradado
    }

def fmcs_lookup(identifier: str) -> Dict[str, Any]:
    """
    Lookup robusto según FMCSA_MODE.
    - real: intenta cadena real; si falla => 502
    - auto: intenta cadena real; si falla => MOCK (degraded=True)
    - mock: mock directo
    Cache simple 24h por identifier.
    """
    key = identifier.strip().upper()
    cached = _fmcsa_cache.get(key)
    if cached and (time.time() - cached["ts"] < CACHE_TTL_SECONDS):
        return cached["data"]

    mode = FMCSA_MODE
    if mode not in ("real", "auto", "mock"):
        mode = "real"

    if mode == "mock":
        metrics["fmcsa_mock_uses"] += 1
        data = _mock_snapshot(identifier)
        _fmcsa_cache[key] = {"ts": time.time(), "data": data}
        return data

    # real / auto
    if not FMCSA_WEBKEY:
        if mode == "real":
            metrics["fmcsa_real_errors"] += 1
            raise RuntimeError("FMCSA_WEBKEY not set, required in 'real' mode")
        # auto sin webkey -> mock
        metrics["fmcsa_mock_uses"] += 1
        data = _mock_snapshot(identifier)
        _fmcsa_cache[key] = {"ts": time.time(), "data": data}
        return data

    try:
        metrics["fmcsa_real_calls"] += 1
        raw, tag = _try_fmcsa_chain(identifier)
        normalized = _normalize_fmcsa_snapshot(raw, identifier, "FMCSA")
        _fmcsa_cache[key] = {"ts": time.time(), "data": normalized}
        return normalized
    except Exception as e:
        metrics["fmcsa_real_errors"] += 1
        if mode == "auto":
            # Degradar a mock, pero marcado
            data = _mock_snapshot(identifier)
            _fmcsa_cache[key] = {"ts": time.time(), "data": data}
            return data
        # real estricto -> error
        raise RuntimeError(str(e))

# -------- Extracción & Sentiment (PoC) --------
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
    if round_idx == 0:
        return round((listed + offer) / 2, 2)
    concession = (listed - offer) * (0.5 * (0.7 ** (round_idx - 1)))
    return round(listed - concession, 2)

# -------------------------
# Rutas
# -------------------------
@app.post("/api/authenticate", dependencies=[Depends(require_api_key)])
def authenticate(carrier: CarrierIn):
    """
    Verifica carrier via FMCSA (real/auto/mock según FMCSA_MODE).
    Elegibilidad: allowToOperate == "Y" y outOfService != "Y".
    En modo auto, si se degrada a mock, el snapshot tendrá degraded=True.
    """
    metrics["calls_total"] += 1
    identifier = carrier.mc_number.strip()
    try:
        snapshot = fmcs_lookup(identifier)
    except Exception as e:
        metrics["auth_failures"] += 1
        raise HTTPException(status_code=502, detail=f"FMCSA lookup failed: {e}")

    allow = str(snapshot.get("allowToOperate", "N")).upper()
    outsvc = str(snapshot.get("outOfService", "N")).upper()
    allowed = (allow == "Y") and (outsvc != "Y")

    # En mock (o auto degradado), permitimos pasar para la demo/PoC
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
      - Máximo 3 rondas (0..3; al llegar a 3 sin aceptar, corta)
    """
    key = f"{payload.mc_number.strip()}:{payload.load_id.strip().upper()}"
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
    total_outcomes = metrics["offers_accepted"] + metrics["offers_rejected"]
    avg_rounds = (metrics["negotiation_rounds_total"] / total_outcomes) if total_outcomes > 0 else None
    return {
        "metrics": metrics,
        "avg_negotiation_rounds": avg_rounds,
        "calls_logged": len(call_results),
        "recent_calls": call_results[-10:]
    }

@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent PoC running - V5"}


