"""
main.py - Inbound Carrier Agent API (FastAPI)
Realistic negotiation + Dashboard with grouped bar chart, date filters, and quick-range buttons

Endpoints:
- POST /api/authenticate
- GET  /api/loads
- POST /api/negotiate
- POST /api/call/result
- GET  /api/metrics                     (requires x-api-key)
- GET  /dashboard                       (public if PUBLIC_DASHBOARD=true)
- GET  /dashboard/data?from=YYYY-MM-DD&to=YYYY-MM-DD (public if PUBLIC_DASHBOARD=true)
"""

import os
import re
import json
import time
from typing import Optional, List, Dict, Any, Tuple
from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import requests

# -------------------------
# Config
# -------------------------
API_KEY = os.getenv("API_KEY", "test-api-key")

FMCSA_WEBKEY = os.getenv("FMCSA_WEBKEY", "")
FMCSA_MODE = os.getenv("FMCSA_MODE", "real").lower()  # 'real' | 'auto' | 'mock'

LOADS_FILE = os.getenv("LOADS_FILE", "./data/loads.json")
MAX_OVER_PCT = float(os.getenv("MAX_OVER_PCT", "0.10"))  # 10% over board rate
PUBLIC_DASHBOARD = os.getenv("PUBLIC_DASHBOARD", "false").lower() == "true"

# -------------------------
# App & memory (PoC)
# -------------------------
app = FastAPI(title="HappyRobot - Inbound Carrier API (Realistic + Dashboard)")

# in-memory stores
negotiations: Dict[str, Dict[str, Any]] = {}  # key = f"{mc}:{load_id}"
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
    offer: Any  # may be number or string

class CallResultIn(BaseModel):
    transcript: str
    mc_number: Optional[str] = None
    load_id: Optional[str] = None
    final_price: Optional[Any] = None
    accepted: Optional[bool] = None

# -------------------------
# Auth middleware
# -------------------------
def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-api-key")

# -------------------------
# Helpers
# -------------------------
def load_loads() -> List[Dict[str, Any]]:
    if not os.path.exists(LOADS_FILE):
        return []
    with open(LOADS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

_num_re = re.compile(r"(-?\d{1,7}(?:\.\d{1,2})?)")

def parse_amount(value: Any) -> float:
    """Robust numeric parser for amounts."""
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

# FMCSA with cache + auto fallback
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
    entry = _fmcsa_cache.get(mc)
    if entry and (time.time() - entry["ts"] < CACHE_TTL_SECONDS):
        return entry["data"]

    if FMCSA_MODE == "mock" or not FMCSA_WEBKEY:
        data = _mock_snapshot(mc)
        _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
        if FMCSA_MODE != "mock":
            metrics["fmcsa_mock_uses"] += 1
        return data

    if FMCSA_MODE == "real":
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
            raise RuntimeError(f"FMCSA lookup failed: {str(e)}")

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
            data = _mock_snapshot(mc)
            metrics["fmcsa_mock_uses"] += 1
            _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
            return data

    data = _mock_snapshot(mc)
    _fmcsa_cache[mc] = {"ts": time.time(), "data": data}
    return data

# realistic negotiation
def compute_counter_down(ceiling: float) -> float:
    return round(ceiling, 2)

# -------------------------
# Core API routes
# -------------------------
@app.post("/api/authenticate", dependencies=[Depends(require_api_key)])
def authenticate(carrier: CarrierIn):
    metrics["calls_total"] += 1
    mc = carrier.mc_number.strip()
    try:
        snapshot = fmcs_lookup_by_mc(mc)
    except Exception as e:
        metrics["auth_failures"] += 1
        raise HTTPException(status_code=502, detail=f"FMCSA lookup failed: {str(e)}")

    allowed = False
    if isinstance(snapshot, dict):
        allow = snapshot.get("allowToOperate") or snapshot.get("allow_to_operate") or snapshot.get("allow")
        out = snapshot.get("outOfService") or snapshot.get("out_of_service")
        if allow in ("Y", "Yes", True, "yes", "y") and out not in ("Y", "Yes", True, "yes", "y"):
            allowed = True
        else:
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
    Realistic negotiation:
      - listed  = board rate
      - ceiling = listed * (1 + MAX_OVER_PCT)
      - Accept if offer <= ceiling (ideally <= listed)
      - If offer > ceiling and round < 3 => counter = ceiling
      - If round >= 3 => no agreement
    """
    key = f"{payload.mc_number}:{payload.load_id}"

    loads = load_loads()
    load = next((l for l in loads if str(l.get("load_id")).strip() == str(payload.load_id).strip()), None)
    if not load:
        raise HTTPException(status_code=404, detail="load not found")

    listed = float(load.get("loadboard_rate", 0))
    ceiling = round(listed * (1.0 + MAX_OVER_PCT), 2)

    state = negotiations.get(key, {"round": 0, "settled": False, "listed": listed, "ceiling": ceiling, "history": []})
    if state["settled"]:
        return {"accepted": True, "price": state.get("price"), "rounds": state["round"], "note": "already settled"}

    offer = parse_amount(payload.offer)
    state["history"].append({"type": "offer", "value": offer, "ts": time.time()})

    if offer <= ceiling:
        state["settled"] = True
        state["price"] = offer
        negotiations[key] = state
        metrics["offers_accepted"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        return {"accepted": True, "price": offer, "round": state["round"], "listed": listed, "ceiling": ceiling}

    if state["round"] >= 3:
        metrics["offers_rejected"] += 1
        metrics["negotiation_rounds_total"] += state["round"]
        state["settled"] = False
        negotiations[key] = state
        return {"accepted": False, "reason": "max rounds reached", "round": state["round"], "listed": listed, "ceiling": ceiling}

    counter = compute_counter_down(ceiling)
    state["round"] += 1
    state["history"].append({"type": "counter", "value": counter, "ts": time.time()})
    negotiations[key] = state
    return {"accepted": False, "counter_offer": counter, "round": state["round"], "listed": listed, "ceiling": ceiling}

@app.post("/api/call/result", dependencies=[Depends(require_api_key)])
def call_result(payload: CallResultIn):
    ent = extract_entities_from_text(payload.transcript or "")
    sentiment = simple_sentiment(payload.transcript)

    final_price_val: Optional[float] = None
    if payload.final_price is not None:
        try:
            final_price_val = parse_amount(payload.final_price)
        except HTTPException:
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

# -------------------------
# Metrics + Dashboard
# -------------------------
def build_metrics_payload(filtered_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    avg_rounds = None
    total = metrics["offers_accepted"] + metrics["offers_rejected"]
    if total > 0:
        avg_rounds = metrics["negotiation_rounds_total"] / total
    return {
        "metrics": metrics,
        "avg_negotiation_rounds": avg_rounds,
        "calls_logged": len(filtered_calls),
        "recent_calls": filtered_calls[-10:],
    }

def _assert_public_dashboard():
    if not PUBLIC_DASHBOARD:
        raise HTTPException(status_code=403, detail="Public dashboard is disabled. Set PUBLIC_DASHBOARD=true to enable.")

def _parse_range_params(from_str: Optional[str], to_str: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    def _valid(d: Optional[str]) -> Optional[str]:
        if not d:
            return None
        if len(d) == 10 and d[4] == '-' and d[7] == '-':
            return d
        return None
    return _valid(from_str), _valid(to_str)

def _filter_calls_by_date(calls: List[Dict[str, Any]], from_date: Optional[str], to_date: Optional[str]) -> List[Dict[str, Any]]:
    out = []
    for r in calls:
        ts = r.get("ts") or ""
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        if from_date and day < from_date:
            continue
        if to_date and day > to_date:
            continue
        out.append(r)
    return out

def _aggregate_by_day(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    agg: Dict[str, Dict[str, int]] = {}
    for r in calls:
        ts = r.get("ts") or ""
        day = ts[:10] if len(ts) >= 10 else None
        if not day:
            continue
        bucket = agg.setdefault(day, {"accepted": 0, "rejected": 0, "total": 0})
        acc = r.get("accepted")
        if acc is True:
            bucket["accepted"] += 1
        elif acc is False:
            bucket["rejected"] += 1
        bucket["total"] += 1
    days = sorted(agg.keys())
    return [{"date": d, **agg[d]} for d in days]

@app.get("/api/metrics", dependencies=[Depends(require_api_key)])
def get_metrics():
    return build_metrics_payload(call_results)

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
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;color:#111}
    h1{margin:0 0 16px}
    .row{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-end}
    .kpis{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}
    .card{border:1px solid #e5e7eb;border-radius:12px;padding:12px 16px;min-width:180px;box-shadow:0 1px 2px rgba(0,0,0,.04)}
    .label{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}
    .value{font-size:24px;font-weight:600}
    .controls{display:flex;gap:12px;align-items:center;margin:8px 0 16px;flex-wrap:wrap}
    .controls input{padding:6px 8px;border:1px solid #e5e7eb;border-radius:8px}
    .controls button{padding:8px 12px;border:1px solid #111;border-radius:8px;background:#111;color:#fff;cursor:pointer}
    .quick{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .quick button{background:#2563eb;border-color:#2563eb}
    canvas{border:1px solid #e5e7eb;border-radius:12px;max-width:100%}
    table{width:100%;border-collapse:collapse;margin-top:12px}
    th,td{border-bottom:1px solid #e5e7eb;padding:8px 10px;text-align:left;font-size:14px}
    th{background:#f9fafb;color:#374151}
    tr:hover{background:#f8fafc}
    .ok{color:#16a34a;font-weight:600}
    .bad{color:#dc2626;font-weight:600}
    .muted{color:#6b7280}
    .legend{display:flex;gap:16px;align-items:center;margin:8px 0}
    .swatch{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:middle}
    .foot{margin-top:16px;color:#6b7280;font-size:12px}
  </style>
</head>
<body>
  <h1>HappyRobot – Inbound Carrier Metrics</h1>

  <div class="controls">
    <div>
      <div class="label">From (YYYY-MM-DD)</div>
      <input id="from" type="date" />
    </div>
    <div>
      <div class="label">To (YYYY-MM-DD)</div>
      <input id="to" type="date" />
    </div>
    <button id="apply">Apply filters</button>
    <span class="muted" id="status"></span>
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
    <div class="card"><div class="label">Avg rounds (global)</div><div id="avg_rounds" class="value">–</div></div>
  </div>

  <div class="legend">
    <span><span class="swatch" style="background:#3b82f6"></span>Accepted</span>
    <span><span class="swatch" style="background:#ef4444"></span>Rejected</span>
  </div>
  <canvas id="chart" width="1100" height="360"></canvas>

  <h2>Recent calls (in range)</h2>
  <table>
    <thead>
      <tr>
        <th>Timestamp (UTC)</th>
        <th>MC</th>
        <th>Load</th>
        <th>Final Price</th>
        <th>Accepted</th>
        <th>Sentiment</th>
        <th class="muted">Extracted</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

  <div class="foot" id="foot">Auto-refreshing…</div>

  <script>
    const elFrom = document.getElementById('from');
    const elTo = document.getElementById('to');
    const elApply = document.getElementById('apply');
    const elStatus = document.getElementById('status');
    const elCalls = document.getElementById('calls_total');
    const elAcc = document.getElementById('accepted');
    const elRej = document.getElementById('rejected');
    const elAvg = document.getElementById('avg_rounds');
    const elTbody = document.getElementById('tbody');
    const elFoot = document.getElementById('foot');
    const canvas = document.getElementById('chart');
    const ctx = canvas.getContext('2d');

    const btn7 = document.getElementById('q7');
    const btn14 = document.getElementById('q14');
    const btn30 = document.getElementById('q30');
    const btnToday = document.getElementById('qToday');
    const btnClear = document.getElementById('qClear');

    function fmtYmdUTC(d){
      const y = d.getUTCFullYear();
      const m = String(d.getUTCMonth()+1).padStart(2,'0');
      const day = String(d.getUTCDate()).padStart(2,'0');
      return `${y}-${m}-${day}`;
    }

    function setRangeDays(days){
      // days=7 -> from=today-6, to=today  (inclusive)
      const now = new Date();
      const to = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
      const from = new Date(to);
      from.setUTCDate(to.getUTCDate() - (days-1));
      elFrom.value = fmtYmdUTC(from);
      elTo.value = fmtYmdUTC(to);
      loadData();
    }

    function setToday(){
      const now = new Date();
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
      const ymd = fmtYmdUTC(d);
      elFrom.value = ymd;
      elTo.value = ymd;
      loadData();
    }

    function clearFilters(){
      elFrom.value = '';
      elTo.value = '';
      loadData();
    }

    function qs(){
      const p = new URLSearchParams();
      if (elFrom.value) p.set('from', elFrom.value);
      if (elTo.value) p.set('to', elTo.value);
      const s = p.toString();
      return s ? ('?' + s) : '';
    }

    async function loadData(){
      const url = '/dashboard/data' + qs();
      elStatus.textContent = 'Loading…';
      const res = await fetch(url);
      if(!res.ok){ elStatus.textContent = 'Error ' + res.status; return; }
      const j = await res.json();
      elStatus.textContent = '';

      // KPIs
      const m = j.metrics || {};
      elCalls.textContent = j.calls_logged ?? 0;
      elAcc.textContent   = j.accepted_in_range ?? 0;
      elRej.textContent   = j.rejected_in_range ?? 0;
      elAvg.textContent   = (j.avg_negotiation_rounds ?? '–');

      // Table
      elTbody.innerHTML = '';
      (j.recent_calls||[]).slice().reverse().forEach(r=>{
        const tr = document.createElement('tr');
        const acc = r.accepted === true ? '<span class="ok">Yes</span>' : (r.accepted === false ? '<span class="bad">No</span>' : '<span class="muted">–</span>');
        const ent = r.entities ? `mc:${r.entities.mc_number ?? ''} price:${r.entities.price ?? ''} load:${r.entities.load_id ?? ''}` : '';
        tr.innerHTML = `
          <td>${r.ts ?? ''}</td>
          <td>${r.mc_number ?? ''}</td>
          <td>${r.load_id ?? ''}</td>
          <td>${(r.final_price ?? '')}</td>
          <td>${acc}</td>
          <td>${r.sentiment ?? ''}</td>
          <td class="muted">${ent}</td>
        `;
        elTbody.appendChild(tr);
      });

      drawChart(j.daily_counts || []);
      elFoot.textContent = 'Updated at ' + (new Date()).toLocaleTimeString();
    }

    function drawChart(rows){
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const W = canvas.width, H = canvas.height;
      const padL = 60, padR = 20, padT = 20, padB = 60;
      const plotW = W - padL - padR;
      const plotH = H - padT - padB;

      const labels = rows.map(r => r.date);
      const acc = rows.map(r => r.accepted || 0);
      const rej = rows.map(r => r.rejected || 0);
      const maxY = Math.max(1, ...acc, ...rej);

      // axes
      ctx.strokeStyle = '#e5e7eb';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, padT);
      ctx.lineTo(padL, padT + plotH);
      ctx.lineTo(padL + plotW, padT + plotH);
      ctx.stroke();

      // y ticks
      ctx.fillStyle = '#6b7280';
      ctx.font = '12px system-ui';
      const ticks = 5;
      for(let i=0;i<=ticks;i++){
        const yVal = Math.round(maxY * i / ticks);
        const y = padT + plotH - (plotH * i / ticks);
        ctx.fillText(String(yVal), padL - 30, y + 4);
        ctx.strokeStyle = '#f3f4f6';
        ctx.beginPath();
        ctx.moveTo(padL, y);
        ctx.lineTo(padL + plotW, y);
        ctx.stroke();
      }

      const n = labels.length;
      if(n === 0){
        ctx.fillStyle = '#6b7280';
        ctx.fillText('No data for selected range', padL + 10, padT + 20);
        return;
      }

      const groupGap = 16;
      const barGap = 8;
      const groupW = Math.max(12, plotW / n - groupGap);
      const barW = Math.max(8, (groupW - barGap) / 2);

      for(let i=0;i<n;i++){
        const x0 = padL + i * (groupW + groupGap);
        // accepted
        const hA = (acc[i] / maxY) * plotH;
        const yA = padT + plotH - hA;
        ctx.fillStyle = '#3b82f6';
        ctx.fillRect(x0, yA, barW, hA);

        // rejected
        const hR = (rej[i] / maxY) * plotH;
        const yR = padT + plotH - hR;
        ctx.fillStyle = '#ef4444';
        ctx.fillRect(x0 + barW + barGap, yR, barW, hR);

        // x labels
        ctx.fillStyle = '#374151';
        ctx.save();
        ctx.translate(x0 + groupW/2, padT + plotH + 16);
        if(n > 10){ ctx.rotate(-Math.PI/6); }
        ctx.textAlign = 'center';
        ctx.fillText(labels[i], 0, 0);
        ctx.restore();
      }
    }

    // Events
    elApply.addEventListener('click', loadData);
    btn7.addEventListener('click', () => setRangeDays(7));
    btn14.addEventListener('click', () => setRangeDays(14));
    btn30.addEventListener('click', () => setRangeDays(30));
    btnToday.addEventListener('click', setToday);
    btnClear.addEventListener('click', clearFilters);

    // Initial load + auto-refresh
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
    acc = sum(1 for r in filtered if r.get("accepted") is True)
    rej = sum(1 for r in filtered if r.get("accepted") is False)

    payload = build_metrics_payload(filtered)
    payload.update({
        "accepted_in_range": acc,
        "rejected_in_range": rej,
        "daily_counts": daily
    })
    return JSONResponse(payload)

# -------------------------
# Root
# -------------------------
@app.get("/")
def root():
    return {"message": "Inbound Carrier Agent running - V10 (dashboard + quick ranges)"}
