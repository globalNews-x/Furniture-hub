"""
IP Checker for Vercel (Flask + Firebase Firestore).

Vercel cannot keep a file between requests, so visits are saved in Firestore,
Firebase's database (free Spark plan, no credit card).

Environment variables (set in Vercel):
    FIREBASE_CREDENTIALS   the whole service-account JSON file, pasted as text
    ADMIN_PASSWORD         password for /admin
    ADMIN_USER             optional, defaults to "admin"
"""
import ipaddress
import json
import math
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import unquote

import requests
from flask import Flask, Response, jsonify, render_template_string, request
from google.cloud import firestore

app = Flask(__name__)

ON_VERCEL = bool(os.environ.get("VERCEL"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

FIELDS = ["country", "region", "city", "latitude", "longitude", "isp"]
_db = None


# ---------- database ----------
def get_db():
    global _db
    if _db is None:
        raw = os.environ.get("FIREBASE_CREDENTIALS")
        if not raw:
            raise RuntimeError("FIREBASE_CREDENTIALS is not set. Add it in the Vercel project settings.")
        _db = firestore.Client.from_service_account_info(json.loads(raw))
    return _db


def visits():
    return get_db().collection("visits")


# ---------- who is visiting, and from where ----------
def get_client_ip():
    if ON_VERCEL:
        # Vercel sets these headers itself and overwrites anything a visitor sends.
        value = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "")
        return value.split(",")[0].strip() or request.remote_addr or "unknown"
    return request.remote_addr or "unknown"


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def location_from_vercel():
    """Vercel works out an approximate location from the IP and sends it as headers."""
    h = request.headers
    if not h.get("x-vercel-ip-country"):
        return None
    return {
        "country": h.get("x-vercel-ip-country"),
        "region": h.get("x-vercel-ip-country-region"),
        "city": unquote(h.get("x-vercel-ip-city", "")) or None,
        "latitude": to_float(h.get("x-vercel-ip-latitude")),
        "longitude": to_float(h.get("x-vercel-ip-longitude")),
        "isp": None,
    }


def lookup_ip(ip):
    """Fallback for local testing: free ipwho.is service. Returns (fields, note)."""
    empty = dict.fromkeys(FIELDS)
    target, note = ip, None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return empty, "Invalid IP address"
    if addr.is_private or addr.is_loopback:
        target = ""
        note = "Local address: location shown is for this computer's public IP (testing only)"
    try:
        data = requests.get("https://ipwho.is/" + target, timeout=4).json()
    except (requests.RequestException, ValueError):
        return empty, "Location service unavailable"
    if not data.get("success"):
        return empty, data.get("message", "Lookup failed")
    conn = data.get("connection") or {}
    return {
        "country": data.get("country"),
        "region": data.get("region"),
        "city": data.get("city"),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "isp": conn.get("isp") or conn.get("org"),
    }, note


# ---------- public page ----------
@app.get("/")
def index():
    ip = get_client_ip()
    loc = location_from_vercel() if ON_VERCEL else None
    note = None
    if loc is None:
        loc, note = lookup_ip(ip)
    ref = visits().document()
    ref.set({
        "visited_at": datetime.now(timezone.utc),
        "ip": ip,
        **{f: loc[f] for f in FIELDS},
        "user_agent": (request.headers.get("User-Agent") or "")[:300],
        "note": note,
        "precise_lat": None,
        "precise_lon": None,
        "precise_accuracy_m": None,
    })
    visit = {"id": ref.id, "ip": ip, "note": note, **loc}
    return render_template_string(INDEX_HTML, visit=visit)


@app.post("/api/precise")
def save_precise():
    """Called only after the visitor clicks the button AND allows location in the browser."""
    body = request.get_json(silent=True) or {}
    try:
        visit_id = str(body["visit_id"])
        lat, lon = float(body["lat"]), float(body["lon"])
        acc = float(body.get("accuracy", 0))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid data"), 400
    if not (visit_id.isalnum() and len(visit_id) <= 40):
        return jsonify(error="Invalid visit"), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and math.isfinite(acc)):
        return jsonify(error="Invalid coordinates"), 400
    ref = visits().document(visit_id)
    snap = ref.get()
    # The IP must match the visit, so nobody can edit someone else's record.
    if not snap.exists or snap.get("ip") != get_client_ip():
        return jsonify(error="Visit not found"), 404
    ref.update({"precise_lat": lat, "precise_lon": lon, "precise_accuracy_m": acc})
    return jsonify(ok=True)


# ---------- admin page ----------
def is_admin():
    auth = request.authorization
    if not (ADMIN_PASSWORD and auth and auth.password):
        return False
    return auth.username == ADMIN_USER and secrets.compare_digest(
        auth.password.encode(), ADMIN_PASSWORD.encode()
    )


@app.get("/admin")
def admin():
    if not ADMIN_PASSWORD:
        return Response("Set the ADMIN_PASSWORD environment variable to enable this page.", 403)
    if not is_admin():
        return Response("Login required.", 401, {"WWW-Authenticate": 'Basic realm="Admin"'})
    docs = visits().order_by("visited_at", direction=firestore.Query.DESCENDING).limit(300).stream()
    rows = [d.to_dict() for d in docs]
    return render_template_string(ADMIN_HTML, rows=rows)


# ---------- templates ----------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IP Checker</title>
<style>
  :root { --bg:#eef1f4; --ink:#14213d; --muted:#5b6678; --line:#cfd6df; --accent:#0f6b8f; --panel:#fff; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#10161f; --ink:#e6ebf2; --muted:#96a2b5; --line:#2b3646; --accent:#5cc0e6; --panel:#172030; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width:640px; margin:0 auto; padding:48px 20px 64px; }
  h1 { font-size:1.1rem; margin:0 0 28px; color:var(--muted); }
  .ip { font:600 clamp(1.8rem,8vw,3rem)/1.1 Consolas, Menlo, monospace; word-break:break-all; margin:0 0 6px; }
  .where { font-size:1.15rem; margin:0 0 4px; }
  .muted { color:var(--muted); margin:0 0 24px; }
  .box { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:24px; }
  .box h2 { font-size:1rem; margin:0 0 6px; }
  .box p { margin:0 0 12px; }
  button { padding:10px 18px; font:inherit; font-weight:600; color:#fff; background:var(--accent); border:0; border-radius:6px; cursor:pointer; }
  @media (prefers-color-scheme: dark) { button { color:#0b1420; } }
  button:disabled { opacity:.6; }
  button:focus-visible, a:focus-visible { outline:3px solid var(--accent); outline-offset:2px; }
  a { color:var(--accent); }
  #msg { margin:12px 0 0; min-height:1.5em; }
  small { color:var(--muted); display:block; }
</style>
</head>
<body>
<main>
  <h1>IP Checker</h1>
  <p class="ip">{{ visit.ip }}</p>
  {% if visit.city or visit.country %}
    <p class="where">{{ [visit.city, visit.region, visit.country] | select | join(", ") }}</p>
    <p class="muted">{% if visit.isp %}Internet provider: {{ visit.isp }}{% endif %}</p>
  {% else %}
    <p class="muted">Location could not be found for this address.</p>
  {% endif %}
  {% if visit.note %}<p class="muted">{{ visit.note }}</p>{% endif %}

  <div class="box">
    <h2>This location is only a rough estimate</h2>
    <p>An IP address usually points to your provider's nearest hub, which can be many kilometres away.
       For a precise result, let your browser share your device location.</p>
    <button id="precise">Share my exact location</button>
    <p id="msg" role="status"></p>
  </div>

  <small>This site saves your IP address, approximate location, browser details and visit time.
  If you share your exact location, that is saved too. Only the site owner can see the saved data.</small>
</main>
<script>
  var visitId = {{ visit.id | tojson }};
  var btn = document.getElementById("precise");
  var msg = document.getElementById("msg");
  var errors = {
    1: "Permission was denied, so nothing was saved.",
    2: "Your device could not work out its position.",
    3: "Finding your position took too long. Try again."
  };
  btn.addEventListener("click", function () {
    if (!navigator.geolocation) { msg.textContent = "This browser does not support location."; return; }
    btn.disabled = true;
    msg.textContent = "Waiting for your permission...";
    navigator.geolocation.getCurrentPosition(function (pos) {
      fetch("/api/precise", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          visit_id: visitId,
          lat: pos.coords.latitude,
          lon: pos.coords.longitude,
          accuracy: pos.coords.accuracy
        })
      }).then(function (res) {
        if (!res.ok) throw new Error("save failed");
        msg.textContent = "Saved. Accurate to about " + Math.round(pos.coords.accuracy) + " metres.";
      }).catch(function () {
        msg.textContent = "Could not save your location. Try again.";
        btn.disabled = false;
      });
    }, function (err) {
      msg.textContent = errors[err.code] || "Could not get your location.";
      btn.disabled = false;
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Saved visits</title>
<style>
  body { margin:0; padding:24px; font:15px/1.4 "Segoe UI", system-ui, sans-serif; color:#14213d; background:#eef1f4; }
  h1 { font-size:1.2rem; }
  .wrap { overflow-x:auto; }
  table { border-collapse:collapse; background:#fff; min-width:900px; }
  th, td { text-align:left; padding:8px 12px; border-bottom:1px solid #cfd6df; vertical-align:top; }
  th { background:#e2e8ef; }
  td.ip { font-family:Consolas, Menlo, monospace; }
</style>
</head>
<body>
  <h1>Saved visits ({{ rows | length }} most recent)</h1>
  <div class="wrap">
  <table>
    <tr><th>Time (UTC)</th><th>IP</th><th>Estimated location</th><th>Provider</th><th>Exact location (shared)</th><th>Browser</th></tr>
    {% for r in rows %}
    <tr>
      <td>{{ r.visited_at.strftime("%Y-%m-%d %H:%M:%S") }}</td>
      <td class="ip">{{ r.ip }}</td>
      <td>{{ [r.city, r.region, r.country] | select | join(", ") or "unknown" }}</td>
      <td>{{ r.isp or "" }}</td>
      <td>{% if r.precise_lat is not none %}
        <a href="https://www.openstreetmap.org/?mlat={{ r.precise_lat }}&amp;mlon={{ r.precise_lon }}&amp;zoom=16" target="_blank" rel="noopener">
        {{ "%.5f"|format(r.precise_lat) }}, {{ "%.5f"|format(r.precise_lon) }}</a>
        (±{{ r.precise_accuracy_m | round | int }} m)
        {% else %}not shared{% endif %}</td>
      <td>{{ r.user_agent }}</td>
    </tr>
    {% endfor %}
  </table>
  </div>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=True)
