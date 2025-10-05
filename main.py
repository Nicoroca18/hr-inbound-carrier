"""
main.py - Inbound Carrier Agent API (V15 Dashboard+)
- Endpoints workflow: /api/authenticate, /api/loads, /api/negotiate, /api/call/result
- Dashboard mejorado: KPIs nuevas, tabla con Board price, columnas más finas y PIE chart (Total $ vs Accepted $)
- Refresh silencioso cada 5s

Notas:
- FMCSA "auto": si hay key intenta real; si falla o no hay key, mock permisivo.
- Negociación realista: carrier pide MÁS que el board; se acepta si oferta <= techo (board * (1 + MAX_OVER_PCT)).
"""

import os
import re
import json
import time
from typing import Optional, List, Dict, Any, Tuple

import requests
from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# -------------------------
# Config
# -------------------------
API_KEY = os.getenv("API_KEY", "test-api-key")

FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY", "")
FMCSA_BASE_URL = "https://mobile.fmcsa.dot.gov/qc/services/"

LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MAX_OVER_PCT = float(os.getenv("MAX_OVER_PCT", "0.10"))  # techo = board * (1 + 10%)
PUBLIC_DASHBOARD = os.getenv("PUBLIC_DASHBOARD", "false").lower() == "true"

# NLP del transcript en /api/call/result
ENABLE_NLP = os.getenv("ENABLE_NLP", "true").lower() == "true"

# -------------------------
# App & Stores
# -------------------------
app = FastAPI(title="HappyRobot - Inbound Carrier API (V15 Dashboard+)")

negotiations: Dict[str, Dict[str, Any]] = {}     # key = f"{mc}:{load_id}"
call_results: List[Dict[str, Any]] = []          # para dashboard

metrics = {
    "calls_total": 0,
    "offers_accepted": 0,
    "offers_rejected": 0,
    "negotiation_rounds_total": 0,
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
    offer: Any  # número o string (p.ej. "$1,600")

class CallResultIn(BaseModel):
    transcript: str
    mc_number: Optional[str] = None
    load_id: Optional[str] = None
    final_price: Optional[Any] = None
    accepted: Optional[bool] = None

# -------------------------
# Auth simple por header
# -------------------------
def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")

# -------------------------
# Utilidades
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

_num_re = re.compile(r"(-?\d{1,7}(?:\.\d{1,2})?)")

def parse_amount(value: Any) -> float:
    """Convierte oferta a float. Soporta '1600', '$1,600', '1600.00'."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "")
        m = _num_re.search(s)
        if m:
            return float(m.group(1))
    raise HTTPException(status_code=422, detail="Invalid offer: must be a numeric amount")

price_re = re.compile(r"\b(?:\$)?\s*(\d{2,6}(?:\.\d{1,2})?)\b")
mc_re = re.compile(r"\bMC(?:\s|#|:)?\s*(\d{4,10})\b", re.IGNORECASE)
loadid_re = re.compile(r"\bL\d{3,}\b", re.IGNORECASE)

def extract_entities_from_text(text: str) -> Dict[str, Any]:
    if not ENABLE_NLP:
        return {}
    t = text or ""
    out: Dict[str, Any] = {}
    if (m := mc_re.search(t)): out["mc_number"] = m.group(1)
    if (m := price_re.search(t.replace(",", ""))): out["price"] = float(m.group(1))
    if (m := loadid_re.search(t)): out["load_id"] = m.group(0)
    return out

def simple_sentiment(text: str) -> str:
    if not ENABLE_NLP:
        return "neutral"
    if not text:
        return "neutral"
    t = text.lower()
    pos = sum(t.count(tok) for tok in ["good", "great", "ok", "thanks", "thank", "yes", "happy", "accept"])
    neg = sum(t.count(tok) for tok in ["no", "not", "reject", "angry", "bad", "hate", "problem", "can't", "cannot"])
    return "positive" if pos > neg else ("negative" if neg > pos else "neutral")

_fmcsa_cache: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 24 * 3600

def _mock_snapshot(mc: str) -> Dict[str, Any]:
    return {
        "mcNumber": mc,
        "legalName": f"Mock Carrier {mc}",
        "allowToOperate": "Y",
        "outOfService": "N",
        "snapshotDate": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "mock"
    }

def fmcs_lookup_by_mc(mc_number: str) -> Dict[str, Any]:
    mc = mc_number.strip()
    entry = _fmcsa_cache.get(mc)
    if entry and (time.time() - entry["ts"] < CACHE_TTL_SECONDS):
        return entry["data"]

    if not FMCSA_WEBKEY:
        data = _mock_snapshot(mc)
        _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
        return data

    try:
        url = f"{FMCSA_BASE_URL}companySnapshot?webKey={FMCSA_WEBKEY}&mcNumber={mc}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data.setdefault("source", "FMCSA")
        else:
            data = _mock_snapshot(mc)
    except Exception:
        data = _mock_snapshot(mc)

    _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
    return data

# -------------------------
# Rutas API
# -------------------------
@app.post("/api/authenticate", dependencies=[Depends(require_api_key)])
def authenticate(carrier: CarrierIn):
    metrics["calls_total"] += 1
    snapshot = fmcs_lookup_by_mc(carrier.mc_number)
    allowed = True
    if isinstance(snapshot, dict):
        allow = snapshot.get("allowToOperate")
        out = snapshot.get("outOfService")
        # Respeta denegación real; mock deja pasar
        if snapshot.get("source") != "mock":
            if str(allow).lower() not in ("y", "yes", "true") or str(out).lower() in ("y", "yes", "true"):
                allowed = False
    return {"eligible": allowed, "carrier": snapshot}

@app.get("/api/loads", response_model=List[LoadOut], dependencies=[Depends(require_api_key)])
def get_loads(
    origin: Optional[str] = None,
    destination: Optional[str] = None,
    max_miles: Optional[float] = None
):
    loads = load_loads()
    def match(l):
        if origin and origin.lower() not in l.get("origin", "").lower(): return False
        if destination and destination.lower() not in l.get("destination", "").lower(): return False
        if max_miles and l.get("miles") and float(l.get("miles")) > float(max_miles): return False
        return True
    filtered = [l for l in loads if match(l)]
    return filtered[:10]

@app.post("/api/negotiate", dependencies=[Depends(require_api_key)])
def negotiate(payload: NegotiateIn):
    """
    Lógica realista:
    - listed = board rate (lo que publicas)
    - ceiling = listed * (1 + MAX_OVER_PCT)
    - El carrier pide MÁS que el board; aceptamos si su oferta <= ceiling.
    - Si oferta > ceiling y aún hay rondas, contra = ceiling.
    - Máx 3 rondas; si no hay acuerdo, rechazamos.
    """
    key = f"{payload.mc_number}:{payload.load_id}"

    loads = load_loads()
    load = next((l for l in loads if str(l.get("load_id")).strip() == str(payload.load_id).strip()), None)
    if not load:
        raise HTTPException(status_code=404, detail="load not found")

    listed = float(load.get("loadboard_rate", 0))
    ceiling = round(listed * (1.0 + MAX_OVER_PCT), 2)

    state = negotiations.get(key, {"round": 0, "settled": False})
    if state["settled"]:
        return {"accepted": True, "price": state.get("price"), "rounds": state["round"], "note": "already settled"}

    offer = parse_amount(payload.offer)

    # Aceptamos si el carrier pide <= techo
    if offer <= ceiling:
        state.update({"settled": True, "price": offer})
        negotiations[key] = state
        metrics["offers_accepted"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        return {"accepted": True, "price": offer, "round": state["round"], "listed": listed, "ceiling": ceiling}

    # Rondas agotadas
    if state["round"] >= 3:
        metrics["offers_rejected"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        state["settled"] = False
        negotiations[key] = state
        return {"accepted": False, "reason": "max rounds reached", "round": state["round"], "listed": listed, "ceiling": ceiling}

    # Contra: techo
    state["round"] += 1
    negotiations[key] = state
    return {"accepted": False, "counter_offer": ceiling, "round": state["round"], "listed": listed, "ceiling": ceiling}

@app.post("/api/call/result", dependencies=[Depends(require_api_key)])
def call_result(payload: CallResultIn):
    """
    Registra el resultado de la llamada para el dashboard y auditoría ligera.
    Guarda board_rate de la carga (si load_id existe) para mostrarlo en la tabla.
    """
    entities = extract_entities_from_text(payload.transcript or "")
    sentiment = simple_sentiment(payload.transcript or "")

    # Normaliza final_price si viene como string/moneda
    final_price_val: Optional[float] = None
    if payload.final_price is not None:
        try:
            final_price_val = parse_amount(payload.final_price)
        except HTTPException:
            final_price_val = None

    # Buscar board_rate por load_id (si existe)
    board_rate_val: Optional[float] = None
    the_load_id = payload.load_id or entities.get("load_id")
    if the_load_id:
        loads = load_loads()
        ld = next((l for l in loads if str(l.get("load_id")).strip() == str(the_load_id).strip()), None)
        if ld:
            try:
                board_rate_val = float(ld.get("loadboard_rate"))
            except Exception:
                board_rate_val = None

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mc_number": payload.mc_number or entities.get("mc_number"),
        "load_id": the_load_id,
        "final_price": final_price_val,
        "accepted": payload.accepted,
        "sentiment": sentiment,
        "board_rate": board_rate_val,     # <-- para la tabla
        "entities": entities,
        "transcript": payload.transcript,
    }
    call_results.append(record)
    return {"ok": True, "summary": record}

# -------------------------
# Dashboard helpers
# -------------------------
def _assert_public_dashboard():
    if not PUBLIC_DASHBOARD:
        raise HTTPException(status_code=403, detail="Public dashboard is disabled. Set PUBLIC_DASHBOARD=true.")

def _parse_range_params(from_str: Optional[str], to_str: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    def _valid(d: Optional[str]) -> Optional[str]:
        if not d: return None
        return d if len(d) == 10 and d[4] == '-' and d[7] == '-' else None
    return _valid(from_str), _valid(to_str)

def _filter_calls_by_date(calls: List[Dict[str, Any]], from_date: Optional[str], to_date: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    for r in calls:
        day = (r.get("ts") or "")[:10]
        if not day: continue
        if from_date and day < from_date: continue
        if to_date and day > to_date: continue
        out.append(r)
    return out

def _aggregate_by_day(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, int]] = {}
    for r in calls:
        day = (r.get("ts") or "")[:10]
        if not day: continue
        bucket = agg.setdefault(day, {"accepted": 0, "rejected": 0})
        acc = r.get("accepted")
        if acc is True: bucket["accepted"] += 1
        elif acc is False: bucket["rejected"] += 1
    return [{"date": d, **agg[d]} for d in sorted(agg.keys())]

def _build_metrics_payload(filtered_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Totales en el rango
    total_accepted = sum(1 for r in filtered_calls if r.get("accepted") is True)
    total_rejected = sum(1 for r in filtered_calls if r.get("accepted") is False)
    calls_in_range = total_accepted + total_rejected

    # Sumas de precios finales
    total_final_sum = sum((r.get("final_price") or 0) for r in filtered_calls if r.get("final_price") is not None)
    accepted_final_sum = sum(
        (r.get("final_price") or 0)
        for r in filtered_calls
        if r.get("accepted") is True and r.get("final_price") is not None
    )

    # Board-match: final_price == board_rate y aceptado
    board_match_acc_count = sum(
        1
        for r in filtered_calls
        if r.get("accepted") is True
        and r.get("final_price") is not None
        and r.get("board_rate") is not None
        and float(r.get("final_price")) == float(r.get("board_rate"))
    )
    board_match_rate_pct = (board_match_acc_count / calls_in_range * 100.0) if calls_in_range > 0 else None

    return {
        "metrics": {
            "calls_total": metrics["calls_total"],
            "offers_accepted": metrics["offers_accepted"],
            "offers_rejected": metrics["offers_rejected"],
        },
        "calls_logged": calls_in_range,
        "recent_calls": filtered_calls[-10:],
        "total_final_sum": round(total_final_sum, 2),
        "accepted_final_sum": round(accepted_final_sum, 2),
        "board_match_accepted_count": board_match_acc_count,
        "board_match_rate_percent": round(board_match_rate_pct, 1) if board_match_rate_pct is not None else None,
    }


# -------------------------
# Dashboard routes
# -------------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    _assert_public_dashboard()
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>HR PoC Metrics</title>
  <style>
    :root{
      --fg:#111; --muted:#6b7280; --line:#e5e7eb; --soft:#f3f4f6;
      --blue:#3b82f6; --red:#ef4444; --green:#16a34a; --indigo:#6366f1;
    }
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;color:var(--fg);background:#fff}
    h1{margin:0 0 16px}
    .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:16px 0}
    .card{border:1px solid var(--line);border-radius:12px;padding:12px 16px;box-shadow:0 1px 2px rgba(0,0,0,.04);background:#fff}
    .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
    .value{font-size:24px;font-weight:600}
    .controls{display:flex;gap:12px;align-items:flex-end;margin:8px 0 16px;flex-wrap:wrap}
    .controls input{padding:6px 8px;border:1px solid var(--line);border-radius:8px}
    .controls button{padding:8px 12px;border:1px solid #111;border-radius:8px;background:#111;color:#fff;cursor:pointer}
    .quick{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .quick button{background:var(--indigo);border-color:var(--indigo);color:#fff;border:none;padding:8px 12px;border-radius:8px;cursor:pointer}
    .row{display:grid;grid-template-columns: 2fr 1fr; gap:16px; align-items:start}
    canvas{border:1px solid var(--line);border-radius:12px;max-width:100%;background:#fff}
    table{width:100%;border-collapse:collapse;margin-top:12px;background:#fff}
    th,td{border-bottom:1px solid var(--line);padding:10px 12px;text-align:left;font-size:14px}
    th{background:#f9fafb;color:#374151}
    tr:hover{background:#f8fafc}
    .ok{color:var(--green);font-weight:600}
    .bad{color:var(--red);font-weight:600}
    .muted{color:var(--muted)}
    .legend{display:flex;gap:16px;align-items:center;margin:8px 0}
    .swatch{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:middle}
  </style>
</head>
<body>
  <h1>HappyRobot – Inbound Carrier Metrics</h1>

  <div class="controls">
    <div>
      <div class="label">From</div>
      <input id="from" type="date" />
    </div>
    <div>
      <div class="label">To</div>
      <input id="to" type="date" />
    </div>
    <button id="apply">Apply filters</button>
    <div class="quick">
      <span class="label" style="margin-left:12px">Quick ranges:</span>
      <button id="q7">Last 7 days</button>
      <button id="q14">Last 14 days</button>
      <button id="q30">Last 30 days</button>
      <button id="qToday">Today</button>
      <button id="qClear">Clear</button>
    </div>
  </div>

  <div class="kpis">
    <div class="card"><div class="label">Calls (in range)</div><div id="calls_total" class="value">–</div></div>
    <div class="card"><div class="label">Accepted</div><div id="accepted" class="value">–</div></div>
    <div class="card"><div class="label">Rejected</div><div id="rejected" class="value">–</div></div>
    <div class="card"><div class="label">Acceptance rate</div><div id="acc_rate" class="value">–</div></div>
    <div class="card"><div class="label">Total $ (all finals)</div><div id="sum_all" class="value">–</div></div>
    <div class="card"><div class="label">Accepted $</div><div id="sum_acc" class="value">–</div></div>
    <div class="card"><div class="label">Board-match accepted (#)</div><div id="board_match_count" class="value">–</div></div>
    <div class="card"><div class="label">Board-match rate</div><div id="board_match_rate" class="value">–</div></div>
  </div>

  <div class="row">
    <div>
      <div class="legend">
        <span><span class="swatch" style="background:#3b82f6"></span>Accepted</span>
        <span><span class="swatch" style="background:#ef4444"></span>Rejected</span>
      </div>
      <canvas id="chartBars" width="1100" height="360"></canvas>
    </div>
    <div>
      <canvas id="chartPie" width="420" height="360"></canvas>
    </div>
  </div>

  <h2>Recent calls (in range)</h2>
  <table>
    <thead>
      <tr>
        <th>Timestamp (UTC)</th>
        <th>MC</th>
        <th>Load</th>
        <th>Board price</th>
        <th>Final price</th>
        <th>Accepted</th>
        <th>Sentiment</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

  <script>
    const elFrom = document.getElementById('from');
    const elTo   = document.getElementById('to');
    const elApply= document.getElementById('apply');

    const elCalls= document.getElementById('calls_total');
    const elAcc  = document.getElementById('accepted');
    const elRej  = document.getElementById('rejected');
    const elRate = document.getElementById('acc_rate');

    const elSumAll = document.getElementById('sum_all');
    const elSumAcc = document.getElementById('sum_acc');
    const elBMatchCount = document.getElementById('board_match_count');
    const elBMatchRate  = document.getElementById('board_match_rate');

    const elTbody= document.getElementById('tbody');

    const ctxBars = document.getElementById('chartBars').getContext('2d');
    const ctxPie  = document.getElementById('chartPie').getContext('2d');

    const btn7 = document.getElementById('q7');
    const btn14= document.getElementById('q14');
    const btn30= document.getElementById('q30');
    const btnToday=document.getElementById('qToday');
    const btnClear=document.getElementById('qClear');

    let isLoading = false; // evita solapes

    function fmtYmdUTC(d){
      const y = d.getUTCFullYear();
      const m = String(d.getUTCMonth()+1).padStart(2,'0');
      const day = String(d.getUTCDate()).padStart(2,'0');
      return `${y}-${m}-${day}`;
    }
    function setRangeDays(days){
      const now = new Date();
      const to = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
      const from = new Date(to);
      from.setUTCDate(to.getUTCDate() - (days-1));
      elFrom.value = fmtYmdUTC(from);
      elTo.value   = fmtYmdUTC(to);
      loadData();
    }
    function setToday(){
      const now = new Date();
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
      const ymd = fmtYmdUTC(d);
      elFrom.value = ymd; elTo.value = ymd;
      loadData();
    }
    function clearFilters(){ elFrom.value=''; elTo.value=''; loadData(); }

    function qs(){
      const p = new URLSearchParams();
      if (elFrom.value) p.set('from', elFrom.value);
      if (elTo.value)   p.set('to', elTo.value);
      const s = p.toString();
      return s ? ('?' + s) : '';
    }

    async function loadData(){
      if (isLoading) return;
      isLoading = true;
      try{
        const res = await fetch('/dashboard/data' + qs());
        if(!res.ok){ isLoading = false; return; }
        const j = await res.json();

        // KPIs
        const calls = j.calls_logged || 0;
        const acc   = j.accepted_in_range || 0;
        const rej   = j.rejected_in_range || 0;
        elCalls.textContent = calls;
        elAcc.textContent   = acc;
        elRej.textContent   = rej;
        elRate.textContent  = calls ? ((acc / calls) * 100).toFixed(1) + '%' : '–';

        elSumAll.textContent = '$' + (j.total_final_sum ?? 0).toLocaleString();
        elSumAcc.textContent = '$' + (j.accepted_final_sum ?? 0).toLocaleString();

        elBMatchCount.textContent = (j.board_match_accepted_count ?? 0);
        elBMatchRate.textContent  = (j.board_match_rate_percent != null) ? (j.board_match_rate_percent.toFixed(1) + '%') : '–';

        // Tabla
        elTbody.innerHTML = '';
        (j.recent_calls||[]).slice().reverse().forEach(r=>{
          const tr = document.createElement('tr');
          const accTxt = r.accepted === true ? '<span class="ok">Yes</span>' : (r.accepted === false ? '<span class="bad">No</span>' : '<span class="muted">–</span>');
          tr.innerHTML = `
            <td>${r.ts ?? ''}</td>
            <td>${r.mc_number ?? ''}</td>
            <td>${r.load_id ?? ''}</td>
            <td>${r.board_rate != null ? ('$'+ Number(r.board_rate).toLocaleString()) : ''}</td>
            <td>${r.final_price != null ? ('$'+ Number(r.final_price).toLocaleString()) : ''}</td>
            <td>${accTxt}</td>
            <td>${r.sentiment ?? ''}</td>
          `;
          elTbody.appendChild(tr);
        });

        drawBars(j.daily_counts || []);
        drawPie(j.total_final_sum || 0, j.accepted_final_sum || 0);
      } finally{
        isLoading = false;
      }
    }

    function drawBars(rows){
      const ctx = ctxBars;
      const W=ctx.canvas.width, H=ctx.canvas.height;
      ctx.clearRect(0,0,W,H);
      const padL=60, padR=20, padT=20, padB=60;
      const plotW=W-padL-padR, plotH=H-padT-padB;

      const labels=rows.map(r=>r.date);
      const acc=rows.map(r=>r.accepted||0);
      const rej=rows.map(r=>r.rejected||0);
      const maxY=Math.max(1, ...acc, ...rej);

      // ejes
      ctx.strokeStyle='#e5e7eb'; ctx.lineWidth=1;
      ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, padT+plotH); ctx.lineTo(padL+plotW, padT+plotH); ctx.stroke();

      // grid Y
      ctx.fillStyle='#6b7280'; ctx.font='12px system-ui';
      const ticks=5;
      for(let i=0;i<=ticks;i++){
        const yVal=Math.round(maxY * i / ticks);
        const y=padT + plotH - (plotH * i / ticks);
        ctx.fillText(String(yVal), padL-30, y+4);
        ctx.strokeStyle='#f3f4f6'; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL+plotW, y); ctx.stroke();
      }

      const n=labels.length;
      if(n===0){ ctx.fillStyle='#6b7280'; ctx.fillText('No data for selected range', padL+10, padT+20); return; }

      // Barras finas y más bonitas
      const groupGap = 24;                 // más espacio entre grupos
      const groupW   = Math.max(18, plotW/n - groupGap);
      const barGap   = 6;
      const barW     = Math.max(6, (groupW - barGap)/2);  // barras más finas

      for(let i=0;i<n;i++){
        const x0=padL + i*(groupW+groupGap);

        // accepted
        const hA=(acc[i]/maxY)*plotH, yA=padT+plotH-hA;
        ctx.fillStyle='#3b82f6';
        ctx.fillRect(x0, yA, barW, hA);

        // rejected
        const hR=(rej[i]/maxY)*plotH, yR=padT+plotH-hR;
        ctx.fillStyle='#ef4444';
        ctx.fillRect(x0+barW+barGap, yR, barW, hR);

        // etiquetas X (rotación si hay muchas)
        ctx.fillStyle='#374151'; ctx.save(); ctx.translate(x0+groupW/2, padT+plotH+16);
        if(n>10){ ctx.rotate(-Math.PI/6); }
        ctx.textAlign='center'; ctx.fillText(labels[i], 0, 0); ctx.restore();
      }
    }

    function drawPie(totalAll, totalAcc){
      const ctx = ctxPie;
      const W=ctx.canvas.width, H=ctx.canvas.height;
      ctx.clearRect(0,0,W,H);

      const cx=W/2, cy=H/2, r=Math.min(W,H)*0.35;
      const values = [totalAcc, Math.max(0, totalAll - totalAcc)];
      const colors = ['#16a34a','#9ca3af']; // aceptado verde, resto gris
      const labels = ['Accepted $','Others $'];

      const sum = values.reduce((a,b)=>a+b,0);
      if(sum <= 0){
        ctx.fillStyle='#6b7280'; ctx.font='14px system-ui';
        ctx.fillText('No amounts in selected range', cx-100, cy);
        return;
      }

      let start = -Math.PI/2;
      for(let i=0;i<values.length;i++){
        const angle = (values[i]/sum) * Math.PI*2;
        ctx.beginPath();
        ctx.moveTo(cx,cy);
        ctx.fillStyle = colors[i];
        ctx.arc(cx,cy,r,start,start+angle);
        ctx.closePath();
        ctx.fill();
        start += angle;
      }

      // Leyenda sencilla
      ctx.font='12px system-ui'; ctx.fillStyle='#374151';
      ctx.fillRect(20, 20, 12, 12); ctx.fillStyle=colors[0]; ctx.fillRect(20,20,12,12);
      ctx.fillStyle='#374151'; ctx.fillText(`${labels[0]}: $${Number(values[0]).toLocaleString()}`, 40, 30);
      ctx.fillStyle='#9ca3af'; ctx.fillRect(20, 40, 12, 12);
      ctx.fillStyle='#374151'; ctx.fillText(`${labels[1]}: $${Number(values[1]).toLocaleString()}`, 40, 50);
    }

    // Eventos
    elApply.addEventListener('click', loadData);
    btn7.addEventListener('click', () => setRangeDays(7));
    btn14.addEventListener('click', () => setRangeDays(14));
    btn30.addEventListener('click', () => setRangeDays(30));
    btnToday.addEventListener('click', setToday);
    btnClear.addEventListener('click', clearFilters);

    // Carga inicial + auto-refresh silencioso
    loadData();
    setInterval(loadData, 5000);
  </script>
</body>
</html>
    """
    return HTMLResponse(html)

@app.get("/dashboard/data", response_class=JSONResponse)
def dashboard_data(
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    _assert_public_dashboard()
    f, t = _parse_range_params(from_date, to_date)
    filtered = _filter_calls_by_date(call_results, f, t)
    daily = _aggregate_by_day(filtered)

    # totales básicos (necesarios para KPIs clásicos)
    acc = sum(1 for r in filtered if r.get("accepted") is True)
    rej = sum(1 for r in filtered if r.get("accepted") is False)

    # payload extendido con nuevas métricas
    payload = _build_metrics_payload(filtered)
    payload.update({
        "accepted_in_range": acc,
        "rejected_in_range": rej,
        "daily_counts": daily
    })
    return JSONResponse(payload)

# -------------------------
# Raíz (health)
# -------------------------
@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent - HappyRobot"}

