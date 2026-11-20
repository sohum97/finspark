#!/usr/bin/env python3
"""Renders a 600x800 greyscale Kindle dashboard (Finsbury Park trains, Tube, weather).

Outputs into ./site:
  dashboard.png   the image the Kindle displays
  api/display     TRMNL-style JSON pointing at the image (also display.json)
  index.html      preview page for your browser

Run `python render.py --demo` to render with sample data (no network or keys).
"""
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

W, H = 600, 800
M = 24  # side margin
TZ = ZoneInfo("Europe/London")
LAT, LON = 51.5642, -0.1065            # Finsbury Park
TFL_STOP = "940GZZLUFPK"               # Finsbury Park Underground
RAIL_FROM, RAIL_TO = "FPK", "MOG"      # Finsbury Park -> Moorgate
OUT = Path(os.environ.get("OUT_DIR", "site"))

# ---- things you may want to tweak -------------------------------------
HOURS = 6                                  # columns in the hourly weather strip
TUBE_LINES = ["victoria", "piccadilly"]    # TfL line ids to show in detail
SHOW_TUBE_ARRIVALS = True                  # next trains under each line's status

# ---------------------------------------------------------------- fonts
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
             "/Library/Fonts", "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts"]


def font(size, bold=False):
    names = (["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"] if bold
             else ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"])
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- data
def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=15,
                     headers={"User-Agent": "kindle-dashboard"})
    r.raise_for_status()
    return r.json()


def tfl_params():
    return {"app_key": os.environ["TFL_APP_KEY"]} if os.environ.get("TFL_APP_KEY") else {}


def fetch_weather(now):
    d = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON, "timezone": "Europe/London",
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "forecast_days": 2})
    rain = d["daily"]["precipitation_probability_max"][0]
    hours = []
    cutoff = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    h = d["hourly"]
    for t, temp, pp, code in zip(h["time"], h["temperature_2m"],
                                 h["precipitation_probability"], h["weather_code"]):
        dt = datetime.fromisoformat(t)
        if dt > cutoff and temp is not None:
            hours.append({"label": dt.strftime("%H:%M"), "temp": round(temp),
                          "rain": pp or 0, "code": code or 0})
        if len(hours) == HOURS:
            break
    return {"temp": round(d["current"]["temperature_2m"]),
            "code": d["current"]["weather_code"],
            "hi": round(d["daily"]["temperature_2m_max"][0]),
            "lo": round(d["daily"]["temperature_2m_min"][0]),
            "rain": 0 if rain is None else rain,
            "hours": hours}


def fetch_rail():
    """National Rail departures via Huxley2 (a JSON wrapper around Darwin/OpenLDBWS)."""
    token = os.environ.get("LDBWS_TOKEN")
    if not token:
        raise RuntimeError("LDBWS_TOKEN is not set")
    base = os.environ.get("HUXLEY_URL", "https://huxley2.azurewebsites.net")
    d = get_json(f"{base}/departures/{RAIL_FROM}/to/{RAIL_TO}/6",
                 {"accessToken": token, "expand": "true"})
    rows = []
    for s in d.get("trainServices") or []:
        etd = s.get("etd") or ""
        if s.get("isCancelled") or etd == "Cancelled":
            status = "Cancelled"
        elif etd == "On time":
            status = "On time"
        elif etd == "Delayed":
            status = "Delayed"
        else:
            status = f"Exp {etd}"
        rows.append({"time": s.get("std", ""), "plat": s.get("platform") or "-",
                     "status": status, "bad": status != "On time"})
    return rows[:5]


def fetch_tube():
    """Next arrivals at Finsbury Park, grouped by line id."""
    arrivals = get_json(f"https://api.tfl.gov.uk/StopPoint/{TFL_STOP}/Arrivals", tfl_params())
    groups = {}
    for a in arrivals:
        if a.get("lineId") in TUBE_LINES:
            groups.setdefault((a["lineId"], a.get("platformName", "")), []).append(a)
    out = {}
    for (line, _plat), items in sorted(groups.items()):
        items.sort(key=lambda x: x["timeToStation"])
        dest = items[0].get("destinationName") or items[0].get("towards") or ""
        out.setdefault(line, []).append({
            "dest": dest.replace(" Underground Station", ""),
            "mins": [max(0, round(i["timeToStation"] / 60)) for i in items[:3]]})
    return out


def fetch_status():
    """Service status for every Tube line, keyed by TfL line id."""
    data = get_json("https://api.tfl.gov.uk/Line/Mode/tube/Status", tfl_params())
    lines = {}
    for ln in data:
        sts = ln.get("lineStatuses") or [{}]
        bad = [s for s in sts if s.get("statusSeverity") != 10]
        s = min(bad, key=lambda x: x.get("statusSeverity", 99)) if bad else sts[0]
        reason = (s.get("reason") or "").strip()
        if ": " in reason[:40]:                 # drop the "VICTORIA LINE: " prefix
            reason = reason.split(": ", 1)[1]
        lines[ln["id"]] = {"name": ln.get("name", ln["id"]),
                           "status": s.get("statusSeverityDescription", "Unknown"),
                           "ok": s.get("statusSeverity") == 10,
                           "reason": reason}
    return lines


def safe(fn, name):
    try:
        return fn()
    except Exception as e:  # keep rendering even if one source is down
        print(f"[warn] {name} failed: {e}", file=sys.stderr)
        return None


def demo_data():
    weather = {"temp": 9, "code": 2, "hi": 11, "lo": 5, "rain": 40, "hours": [
        {"label": "13:00", "temp": 10, "rain": 10, "code": 2},
        {"label": "14:00", "temp": 11, "rain": 20, "code": 3},
        {"label": "15:00", "temp": 11, "rain": 55, "code": 61},
        {"label": "16:00", "temp": 10, "rain": 70, "code": 63},
        {"label": "17:00", "temp": 8, "rain": 40, "code": 80},
        {"label": "18:00", "temp": 7, "rain": 15, "code": 0}]}
    rail = [{"time": "07:42", "plat": "14", "status": "On time", "bad": False},
            {"time": "07:50", "plat": "14", "status": "Exp 07:54", "bad": True},
            {"time": "07:58", "plat": "14", "status": "On time", "bad": False},
            {"time": "08:06", "plat": "14", "status": "Cancelled", "bad": True}]
    tube = {"victoria": [{"dest": "Walthamstow Central", "mins": [1, 4, 7]},
                         {"dest": "Brixton", "mins": [3, 6, 9]}],
            "piccadilly": [{"dest": "Cockfosters", "mins": [2, 7, 11]},
                           {"dest": "Heathrow Terminal 5", "mins": [4, 9]}]}
    status = {"victoria": {"name": "Victoria", "status": "Good Service", "ok": True, "reason": ""},
              "piccadilly": {"name": "Piccadilly", "status": "Minor Delays", "ok": False,
                             "reason": "Minor delays between Arnos Grove and Cockfosters due to "
                                       "an earlier signal failure."},
              "northern": {"name": "Northern", "status": "Part Suspended", "ok": False, "reason": ""},
              "central": {"name": "Central", "status": "Good Service", "ok": True, "reason": ""}}
    return weather, rail, tube, status


# ---------------------------------------------------------------- drawing
def describe(code):
    if code == 0:
        return "Clear", "sun"
    if code == 1:
        return "Mostly clear", "partly"
    if code == 2:
        return "Partly cloudy", "partly"
    if code == 3:
        return "Overcast", "cloud"
    if code in (45, 48):
        return "Fog", "cloud"
    if code in (51, 53, 55, 56, 57):
        return "Drizzle", "rain"
    if code in (61, 63, 65, 66, 67):
        return "Rain", "rain"
    if code in (80, 81, 82):
        return "Showers", "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "Snow", "snow"
    if code in (95, 96, 99):
        return "Thunderstorm", "rain"
    return "Unknown", "cloud"


def draw_icon(d, kind, x, y, s):
    k = s / 118.0
    lw = max(2, round(5 * k))

    def sun(cx, cy, r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=lw)
        for i in range(8):
            a = math.radians(i * 45)
            d.line([cx + math.cos(a) * (r + 7 * k), cy + math.sin(a) * (r + 7 * k),
                    cx + math.cos(a) * (r + 18 * k), cy + math.sin(a) * (r + 18 * k)],
                   fill=0, width=lw)

    def cloud(x0, y0, w):
        g = 70
        d.ellipse([x0, y0 + w * .25, x0 + w * .5, y0 + w * .65], fill=g)
        d.ellipse([x0 + w * .2, y0, x0 + w * .7, y0 + w * .55], fill=g)
        d.ellipse([x0 + w * .45, y0 + w * .2, x0 + w, y0 + w * .65], fill=g)
        d.rectangle([x0 + w * .25, y0 + w * .4, x0 + w * .75, y0 + w * .65], fill=g)

    if kind == "sun":
        sun(x + s // 2, y + s // 2, s * .24)
    elif kind == "partly":
        sun(x + s * .35, y + s * .35, s * .16)
        cloud(x + s * .25, y + s * .3, s * .75)
    elif kind == "cloud":
        cloud(x, y + s * .15, s)
    elif kind == "rain":
        cloud(x, y, s)
        for i in range(4):
            px = x + s * (.22 + i * .17)
            d.line([px, y + s * .72, px - 8 * k, y + s * .95], fill=0, width=lw)
    elif kind == "snow":
        cloud(x, y, s)
        for i in range(4):
            px = x + s * (.22 + i * .17)
            d.ellipse([px - 4 * k, y + s * .78, px + 4 * k, y + s * .78 + 8 * k], fill=0)


def fit(d, text, f, maxw):
    while d.textlength(text, font=f) > maxw and len(text) > 1:
        text = text[:-2] + "…"
    return text


def wrap(d, text, f, maxw, maxlines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=f) <= maxw:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > maxlines:
        lines = lines[:maxlines]
        lines[-1] = fit(d, lines[-1] + "…", f, maxw)
    return lines


def rtext(d, x_right, y, text, f, fill=0):
    d.text((x_right - d.textlength(text, font=f), y), text, font=f, fill=fill)


def ctext(d, cx, y, text, f, fill=0):
    d.text((cx - d.textlength(text, font=f) / 2, y), text, font=f, fill=fill)


def rule(d, y, w=3, fill=0):
    d.line([M, y, W - M, y], fill=fill, width=w)


def draw_weather(d, weather):
    if not weather:
        d.text((M, 100), "Weather unavailable", font=font(28), fill=110)
        return
    label, icon = describe(weather["code"])
    draw_icon(d, icon, M, 54, 96)
    d.text((140, 42), f"{weather['temp']}°", font=font(72, True), fill=0)
    d.text((140, 126), label, font=font(24), fill=0)
    d.text((375, 58), f"High {weather['hi']}°", font=font(24, True), fill=0)
    d.text((375, 90), f"Low {weather['lo']}°", font=font(24), fill=0)
    d.text((375, 122), f"Rain {weather['rain']}%", font=font(24), fill=0)

    hours = weather.get("hours") or []
    if hours:
        colw = (W - 2 * M) / len(hours)
        for i, hr in enumerate(hours):
            cx = M + colw * i + colw / 2
            _, ic = describe(hr["code"])
            ctext(d, cx, 166, hr["label"], font(19), fill=60)
            draw_icon(d, ic, cx - 22, 190, 44)
            ctext(d, cx, 236, f"{hr['temp']}°", font(26, True))
            ctext(d, cx, 268, f"{hr['rain']}%", font(19), fill=60)
            if i:
                d.line([M + colw * i, 168, M + colw * i, 292], fill=190, width=1)


def draw_rail(d, rail):
    d.text((M, 306), "Finsbury Park → Moorgate", font=font(26, True), fill=0)
    y = 344
    if rail is None:
        d.text((M, y), "Train times unavailable", font=font(26), fill=110)
    elif not rail:
        d.text((M, y), "No services listed", font=font(26), fill=110)
    else:
        for r in rail[:4]:
            d.text((M, y), r["time"], font=font(34, True), fill=0)
            d.text((190, y + 6), f"Plat {r['plat']}", font=font(24), fill=0)
            rtext(d, W - M, y + 6, r["status"], font(24, r["bad"]), fill=0)
            y += 44


def draw_tube(d, tube, status):
    d.text((M, 538), "Tube · Finsbury Park", font=font(26, True), fill=0)
    if tube is None and status is None:
        d.text((M, 578), "Tube information unavailable", font=font(26), fill=110)
        return
    y, limit = 576, 764
    for lid in TUBE_LINES:
        st = (status or {}).get(lid)
        name = st["name"] if st else lid.title()
        d.text((M, y), name, font=font(26, True), fill=0)
        if st:
            rtext(d, W - M, y + 2, st["status"], font(24, not st["ok"]), fill=0)
        y += 34
        if st and not st["ok"] and st["reason"]:
            for line in wrap(d, st["reason"], font(18), W - 2 * M, 2):
                d.text((M, y), line, font=font(18), fill=70)
                y += 22
        if SHOW_TUBE_ARRIVALS and tube:
            for t in tube.get(lid, []):
                if y + 26 > limit:
                    break
                mins = ", ".join("due" if m == 0 else str(m) for m in t["mins"]) + " min"
                fm = font(22, True)
                left = fit(d, f"→ {t['dest']}", font(22), W - 2 * M - d.textlength(mins, font=fm) - 16)
                d.text((M + 8, y), left, font=font(22), fill=0)
                rtext(d, W - M, y, mins, fm, fill=0)
                y += 26
        y += 8
    if status:
        others = sorted(v["name"] for k, v in status.items() if k not in TUBE_LINES and not v["ok"])
        msg = "Other lines: good service" if not others else "Disruption: " + ", ".join(others)
        d.text((M, 770), fit(d, msg, font(18), W - 2 * M), font=font(18), fill=70)


def render(weather, rail, tube, status, now):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.text((M, 12), f"{now.strftime('%A')} {now.day} {now.strftime('%B')}", font=font(28, True), fill=0)
    rtext(d, W - M, 20, f"upd {now.strftime('%H:%M')}", font(18), fill=110)
    draw_weather(d, weather)
    rule(d, 296)
    draw_rail(d, rail)
    rule(d, 528)
    draw_tube(d, tube, status)
    return img


# ---------------------------------------------------------------- output
def public_base():
    if os.environ.get("PUBLIC_BASE_URL"):
        return os.environ["PUBLIC_BASE_URL"].rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return "http://localhost:8000"


def main():
    now = datetime.now(TZ)
    if "--demo" in sys.argv:
        weather, rail, tube, status = demo_data()
    else:
        weather = safe(lambda: fetch_weather(now), "weather")
        rail = safe(fetch_rail, "rail")
        tube = safe(fetch_tube, "tube arrivals")
        status = safe(fetch_status, "tube status")

    img = render(weather, rail, tube, status, now)
    (OUT / "api").mkdir(parents=True, exist_ok=True)
    img.save(OUT / "dashboard.png", optimize=True)

    stamp = now.strftime("%Y%m%d%H%M")
    meta = {
        "status": 0,
        "image_url": f"{public_base()}/dashboard.png?v={stamp}",
        "filename": f"dash-{stamp}.png",
        "refresh_rate": int(os.environ.get("REFRESH_SECONDS", "600")),
        "update_firmware": False,
        "firmware_url": None,
        "reset_firmware": False,
    }
    for p in (OUT / "api" / "display", OUT / "display.json"):
        p.write_text(json.dumps(meta))
    (OUT / "index.html").write_text(
        '<!doctype html><meta name="viewport" content="width=device-width">'
        '<img src="dashboard.png" style="max-width:100%;border:1px solid #888">')
    print("wrote", OUT / "dashboard.png")


if __name__ == "__main__":
    main()
