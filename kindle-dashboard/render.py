#!/usr/bin/env python3
"""Renders a 600x800 greyscale Kindle dashboard (Finsbury Park trains + weather).

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


def fetch_weather():
    d = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON, "timezone": "Europe/London",
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1})
    rain = d["daily"]["precipitation_probability_max"][0]
    return {"temp": round(d["current"]["temperature_2m"]),
            "code": d["current"]["weather_code"],
            "hi": round(d["daily"]["temperature_2m_max"][0]),
            "lo": round(d["daily"]["temperature_2m_min"][0]),
            "rain": 0 if rain is None else rain}


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
    params = {}
    if os.environ.get("TFL_APP_KEY"):
        params["app_key"] = os.environ["TFL_APP_KEY"]
    arrivals = get_json(f"https://api.tfl.gov.uk/StopPoint/{TFL_STOP}/Arrivals", params)
    groups = {}
    for a in arrivals:
        if a.get("lineName") in ("Victoria", "Piccadilly"):
            groups.setdefault((a["lineName"], a.get("platformName", "")), []).append(a)
    out = []
    for (line, _plat), items in sorted(groups.items()):
        items.sort(key=lambda x: x["timeToStation"])
        dest = items[0].get("destinationName") or items[0].get("towards") or ""
        dest = dest.replace(" Underground Station", "")
        out.append({"line": line, "dest": dest,
                    "mins": [max(0, round(i["timeToStation"] / 60)) for i in items[:3]]})
    return out


def safe(fn, name):
    try:
        return fn()
    except Exception as e:  # keep rendering even if one source is down
        print(f"[warn] {name} failed: {e}", file=sys.stderr)
        return None


DEMO = (
    {"temp": 9, "code": 2, "hi": 11, "lo": 5, "rain": 40},
    [{"time": "07:42", "plat": "14", "status": "On time", "bad": False},
     {"time": "07:50", "plat": "14", "status": "Exp 07:54", "bad": True},
     {"time": "07:58", "plat": "14", "status": "On time", "bad": False},
     {"time": "08:06", "plat": "14", "status": "Cancelled", "bad": True},
     {"time": "08:14", "plat": "14", "status": "On time", "bad": False}],
    [{"line": "Piccadilly", "dest": "Cockfosters", "mins": [2, 7, 11]},
     {"line": "Piccadilly", "dest": "Heathrow Terminal 5", "mins": [4, 9]},
     {"line": "Victoria", "dest": "Walthamstow Central", "mins": [1, 4, 7]},
     {"line": "Victoria", "dest": "Brixton", "mins": [3, 6, 9]}],
)

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
    def sun(cx, cy, r):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=5)
        for i in range(8):
            a = math.radians(i * 45)
            d.line([cx + math.cos(a) * (r + 7), cy + math.sin(a) * (r + 7),
                    cx + math.cos(a) * (r + 18), cy + math.sin(a) * (r + 18)], fill=0, width=5)

    def cloud(x0, y0, w):
        g = 70
        d.ellipse([x0, y0 + w * .25, x0 + w * .5, y0 + w * .65], fill=g)
        d.ellipse([x0 + w * .2, y0, x0 + w * .7, y0 + w * .55], fill=g)
        d.ellipse([x0 + w * .45, y0 + w * .2, x0 + w, y0 + w * .65], fill=g)
        d.rectangle([x0 + w * .25, y0 + w * .4, x0 + w * .75, y0 + w * .65], fill=g)

    if kind == "sun":
        sun(x + s // 2, y + s // 2, int(s * .24))
    elif kind == "partly":
        sun(x + int(s * .35), y + int(s * .35), int(s * .16))
        cloud(x + s * .25, y + s * .3, s * .75)
    elif kind == "cloud":
        cloud(x, y + s * .15, s)
    elif kind == "rain":
        cloud(x, y, s)
        for i in range(4):
            px = x + s * (.22 + i * .17)
            d.line([px, y + s * .72, px - 8, y + s * .95], fill=0, width=4)
    elif kind == "snow":
        cloud(x, y, s)
        for i in range(4):
            px = x + s * (.22 + i * .17)
            d.ellipse([px - 4, y + s * .78, px + 4, y + s * .78 + 8], fill=0)


def fit(d, text, f, maxw):
    while d.textlength(text, font=f) > maxw and len(text) > 1:
        text = text[:-2] + "…"
    return text


def rtext(d, x_right, y, text, f, fill=0):
    d.text((x_right - d.textlength(text, font=f), y), text, font=f, fill=fill)


def rule(d, y, w=3, fill=0):
    d.line([M, y, W - M, y], fill=fill, width=w)


def render(weather, rail, tube, now):
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)

    # header
    d.text((M, 16), f"{now.strftime('%A')} {now.day} {now.strftime('%B')}", font=font(28, True), fill=0)
    rtext(d, W - M, 24, f"upd {now.strftime('%H:%M')}", font(18), fill=110)

    # weather
    if weather:
        label, icon = describe(weather["code"])
        draw_icon(d, icon, M, 62, 118)
        d.text((170, 52), f"{weather['temp']}°", font=font(84, True), fill=0)
        d.text((170, 150), label, font=font(26), fill=0)
        d.text((375, 78), f"High {weather['hi']}°", font=font(26, True), fill=0)
        d.text((375, 112), f"Low {weather['lo']}°", font=font(26), fill=0)
        d.text((375, 146), f"Rain {weather['rain']}%", font=font(26), fill=0)
    else:
        d.text((M, 100), "Weather unavailable", font=font(28), fill=110)
    rule(d, 200)

    # National Rail
    d.text((M, 214), "Finsbury Park → Moorgate", font=font(28, True), fill=0)
    y = 262
    if rail is None:
        d.text((M, y), "Train times unavailable", font=font(26), fill=110)
    elif not rail:
        d.text((M, y), "No services listed", font=font(26), fill=110)
    else:
        for r in rail[:5]:
            d.text((M, y), r["time"], font=font(40, True), fill=0)
            d.text((190, y + 8), f"Plat {r['plat']}", font=font(26), fill=0)
            rtext(d, W - M, y + 8, r["status"], font(26, r["bad"]), fill=0)
            y += 52
    rule(d, 530)

    # Tube
    d.text((M, 544), "Tube · Finsbury Park", font=font(28, True), fill=0)
    y = 592
    if tube is None:
        d.text((M, y), "Tube times unavailable", font=font(26), fill=110)
    elif not tube:
        d.text((M, y), "No arrivals listed", font=font(26), fill=110)
    else:
        for t in tube[:4]:
            mins = ", ".join("due" if m == 0 else str(m) for m in t["mins"]) + " min"
            fm = font(26, True)
            right_w = d.textlength(mins, font=fm)
            left = fit(d, f"{t['line']} to {t['dest']}", font(24), W - 2 * M - right_w - 16)
            d.text((M, y + 3), left, font=font(24), fill=0)
            rtext(d, W - M, y, mins, fm, fill=0)
            y += 48
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
        weather, rail, tube = DEMO
    else:
        weather, rail, tube = safe(fetch_weather, "weather"), safe(fetch_rail, "rail"), safe(fetch_tube, "tube")

    img = render(weather, rail, tube, now)
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
