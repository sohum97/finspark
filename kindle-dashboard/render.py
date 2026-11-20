#!/usr/bin/env python3
"""Render the 600x800 greyscale Finsbury Park Kindle dashboard.

Shows local weather, upcoming Google Calendar events, and Finsbury Park Tube
arrivals/status. Calendar access uses the private GOOGLE_CALENDAR_ICS_URL
GitHub Actions secret; the secret URL is never written into generated files.

Run `python render.py --demo` to render sample data without network access.
"""
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont
from icalendar import Calendar
import recurring_ical_events

W, H = 600, 800
M = 24
TZ = ZoneInfo("Europe/London")
LAT, LON = 51.5642, -0.1065
TFL_STOP = "940GZZLUFPK"
OUT = Path(os.environ.get("OUT_DIR", "site"))
HOURS = 6
TUBE_LINES = ["victoria", "piccadilly"]
SHOW_TUBE_ARRIVALS = True
MAX_CALENDAR_EVENTS = 4

FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
             "/Library/Fonts", "/System/Library/Fonts/Supplemental", "C:/Windows/Fonts"]


def font(size, bold=False):
    names = (["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf"] if bold
             else ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"])
    for directory in FONT_DIRS:
        for name in names:
            path = Path(directory) / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def get_json(url, params=None):
    response = requests.get(url, params=params, timeout=15,
                            headers={"User-Agent": "kindle-dashboard"})
    response.raise_for_status()
    return response.json()


def tfl_params():
    return {"app_key": os.environ["TFL_APP_KEY"]} if os.environ.get("TFL_APP_KEY") else {}


def fetch_weather(now):
    data = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON, "timezone": "Europe/London",
        "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "hourly": "temperature_2m,precipitation_probability,weather_code",
        "forecast_days": 2})
    rain = data["daily"]["precipitation_probability_max"][0]
    hours = []
    cutoff = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hourly = data["hourly"]
    for stamp, temp, probability, code in zip(
            hourly["time"], hourly["temperature_2m"],
            hourly["precipitation_probability"], hourly["weather_code"]):
        dt = datetime.fromisoformat(stamp)
        if dt > cutoff and temp is not None:
            hours.append({"label": dt.strftime("%H:%M"), "temp": round(temp),
                          "rain": probability or 0, "code": code or 0})
        if len(hours) == HOURS:
            break
    return {"temp": round(data["current"]["temperature_2m"]),
            "code": data["current"]["weather_code"],
            "hi": round(data["daily"]["temperature_2m_max"][0]),
            "lo": round(data["daily"]["temperature_2m_min"][0]),
            "rain": 0 if rain is None else rain, "hours": hours}


def fetch_calendar(now):
    """Fetch up to four remaining-today/tomorrow events from a private iCal feed."""
    feed_url = os.environ.get("GOOGLE_CALENDAR_ICS_URL")
    if not feed_url:
        raise RuntimeError("GOOGLE_CALENDAR_ICS_URL is not set")
    response = requests.get(feed_url, timeout=20,
                            headers={"User-Agent": "kindle-dashboard"})
    response.raise_for_status()
    calendar = Calendar.from_ical(response.content)
    start = now
    end = datetime.combine(now.date() + timedelta(days=2), time.min, TZ)
    events = recurring_ical_events.of(calendar).between(start, end)
    result = []
    for component in events:
        if component.name != "VEVENT":
            continue
        raw_start = component.get("DTSTART")
        if raw_start is None:
            continue
        start_value = raw_start.dt
        all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)
        if all_day:
            event_date = start_value
            if event_date < now.date() or event_date > now.date() + timedelta(days=1):
                continue
            sort_dt = datetime.combine(event_date, time.min, TZ)
            label = "All day"
        else:
            if start_value.tzinfo is None:
                start_value = start_value.replace(tzinfo=TZ)
            start_value = start_value.astimezone(TZ)
            if start_value < now or start_value.date() > now.date() + timedelta(days=1):
                continue
            sort_dt = start_value
            label = start_value.strftime("%H:%M")
        title = str(component.get("SUMMARY", "Untitled event")).replace("\\n", " ").strip()
        result.append({"sort": sort_dt, "date": sort_dt.date(), "time": label,
                       "title": title or "Untitled event", "all_day": all_day})
    result.sort(key=lambda event: event["sort"])
    return [{key: value for key, value in event.items() if key != "sort"}
            for event in result[:MAX_CALENDAR_EVENTS]]


def fetch_tube():
    arrivals = get_json(f"https://api.tfl.gov.uk/StopPoint/{TFL_STOP}/Arrivals", tfl_params())
    groups = {}
    for arrival in arrivals:
        if arrival.get("lineId") in TUBE_LINES:
            groups.setdefault((arrival["lineId"], arrival.get("platformName", "")), []).append(arrival)
    output = {}
    for (line, _platform), items in sorted(groups.items()):
        items.sort(key=lambda item: item["timeToStation"])
        destination = items[0].get("destinationName") or items[0].get("towards") or ""
        output.setdefault(line, []).append({
            "dest": destination.replace(" Underground Station", ""),
            "mins": [max(0, round(item["timeToStation"] / 60)) for item in items[:3]]})
    return output


def fetch_status():
    data = get_json("https://api.tfl.gov.uk/Line/Mode/tube/Status", tfl_params())
    lines = {}
    for line in data:
        statuses = line.get("lineStatuses") or [{}]
        disrupted = [item for item in statuses if item.get("statusSeverity") != 10]
        status = min(disrupted, key=lambda item: item.get("statusSeverity", 99)) if disrupted else statuses[0]
        reason = (status.get("reason") or "").strip()
        if ": " in reason[:40]:
            reason = reason.split(": ", 1)[1]
        lines[line["id"]] = {"name": line.get("name", line["id"]),
                             "status": status.get("statusSeverityDescription", "Unknown"),
                             "ok": status.get("statusSeverity") == 10,
                             "reason": reason}
    return lines


def safe(fn, name):
    try:
        return fn()
    except Exception as exc:
        print(f"[warn] {name} failed: {exc}", file=sys.stderr)
        return None


def demo_data(now):
    weather = {"temp": 9, "code": 2, "hi": 11, "lo": 5, "rain": 40, "hours": [
        {"label": f"{(now.hour + i + 1) % 24:02d}:00", "temp": 10 + i % 2,
         "rain": 10 + i * 8, "code": [2, 3, 61, 63, 80, 0][i]} for i in range(6)]}
    calendar = [
        {"date": now.date(), "time": "15:30", "title": "Project catch-up", "all_day": False},
        {"date": now.date(), "time": "18:00", "title": "Dinner with friends", "all_day": False},
        {"date": now.date() + timedelta(days=1), "time": "All day", "title": "Annual leave", "all_day": True},
        {"date": now.date() + timedelta(days=1), "time": "10:00", "title": "Team meeting", "all_day": False}]
    tube = {"victoria": [{"dest": "Walthamstow Central", "mins": [1, 4, 7]},
                         {"dest": "Brixton", "mins": [3, 6, 9]}],
            "piccadilly": [{"dest": "Cockfosters", "mins": [2, 7, 11]},
                           {"dest": "Heathrow Terminal 5", "mins": [4, 9]}]}
    status = {"victoria": {"name": "Victoria", "status": "Good Service", "ok": True, "reason": ""},
              "piccadilly": {"name": "Piccadilly", "status": "Minor Delays", "ok": False,
                             "reason": "Minor delays between Arnos Grove and Cockfosters due to an earlier signal failure."},
              "northern": {"name": "Northern", "status": "Part Suspended", "ok": False, "reason": ""},
              "central": {"name": "Central", "status": "Good Service", "ok": True, "reason": ""}}
    return weather, calendar, tube, status


def describe(code):
    if code == 0: return "Clear", "sun"
    if code == 1: return "Mostly clear", "partly"
    if code == 2: return "Partly cloudy", "partly"
    if code == 3: return "Overcast", "cloud"
    if code in (45, 48): return "Fog", "cloud"
    if code in (51, 53, 55, 56, 57): return "Drizzle", "rain"
    if code in (61, 63, 65, 66, 67): return "Rain", "rain"
    if code in (80, 81, 82): return "Showers", "rain"
    if code in (71, 73, 75, 77, 85, 86): return "Snow", "snow"
    if code in (95, 96, 99): return "Thunderstorm", "rain"
    return "Unknown", "cloud"


def draw_icon(d, kind, x, y, size):
    scale = size / 118.0
    width = max(2, round(5 * scale))
    def sun(cx, cy, radius):
        d.ellipse([cx-radius, cy-radius, cx+radius, cy+radius], outline=0, width=width)
        for i in range(8):
            angle = math.radians(i * 45)
            d.line([cx+math.cos(angle)*(radius+7*scale), cy+math.sin(angle)*(radius+7*scale),
                    cx+math.cos(angle)*(radius+18*scale), cy+math.sin(angle)*(radius+18*scale)], fill=0, width=width)
    def cloud(x0, y0, w):
        grey = 70
        d.ellipse([x0, y0+w*.25, x0+w*.5, y0+w*.65], fill=grey)
        d.ellipse([x0+w*.2, y0, x0+w*.7, y0+w*.55], fill=grey)
        d.ellipse([x0+w*.45, y0+w*.2, x0+w, y0+w*.65], fill=grey)
        d.rectangle([x0+w*.25, y0+w*.4, x0+w*.75, y0+w*.65], fill=grey)
    if kind == "sun": sun(x+size//2, y+size//2, size*.24)
    elif kind == "partly":
        sun(x+size*.35, y+size*.35, size*.16); cloud(x+size*.25, y+size*.3, size*.75)
    elif kind == "cloud": cloud(x, y+size*.15, size)
    elif kind == "rain":
        cloud(x, y, size)
        for i in range(4):
            px = x + size*(.22+i*.17)
            d.line([px, y+size*.72, px-8*scale, y+size*.95], fill=0, width=width)
    elif kind == "snow":
        cloud(x, y, size)
        for i in range(4):
            px = x + size*(.22+i*.17)
            d.ellipse([px-4*scale, y+size*.78, px+4*scale, y+size*.78+8*scale], fill=0)


def fit(d, text, f, max_width):
    text = str(text)
    while d.textlength(text, font=f) > max_width and len(text) > 1:
        text = text[:-2] + "…"
    return text


def wrap(d, text, f, max_width, max_lines):
    words, lines, current = str(text).split(), [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if d.textlength(trial, font=f) <= max_width:
            current = trial
        else:
            if current: lines.append(current)
            current = word
    if current: lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = fit(d, lines[-1] + "…", f, max_width)
    return lines


def rtext(d, x_right, y, text, f, fill=0):
    d.text((x_right-d.textlength(text, font=f), y), text, font=f, fill=fill)


def ctext(d, cx, y, text, f, fill=0):
    d.text((cx-d.textlength(text, font=f)/2, y), text, font=f, fill=fill)


def rule(d, y, width=3, fill=0):
    d.line([M, y, W-M, y], fill=fill, width=width)


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
        colw = (W-2*M)/len(hours)
        for i, hour in enumerate(hours):
            cx = M+colw*i+colw/2
            _, weather_icon = describe(hour["code"])
            ctext(d, cx, 166, hour["label"], font(19), 60)
            draw_icon(d, weather_icon, cx-22, 190, 44)
            ctext(d, cx, 236, f"{hour['temp']}°", font(26, True))
            ctext(d, cx, 268, f"{hour['rain']}%", font(19), 60)
            if i: d.line([M+colw*i, 168, M+colw*i, 292], fill=190, width=1)


def draw_calendar(d, events, now):
    d.text((M, 306), "Calendar · Today & tomorrow", font=font(25, True), fill=0)
    if events is None:
        d.text((M, 344), "Calendar unavailable", font=font(22), fill=110)
        return
    if not events:
        d.text((M, 344), "No more events today or tomorrow", font=font(21), fill=100)
        return
    y = 342
    today = now.date()
    for event in events[:MAX_CALENDAR_EVENTS]:
        day_label = "Today" if event["date"] == today else "Tomorrow"
        time_label = event["time"]
        prefix = f"{day_label} {time_label}"
        d.text((M, y), prefix, font=font(19, True), fill=70)
        title_x = 190
        title = fit(d, event["title"], font(21), W-M-title_x)
        d.text((title_x, y-2), title, font=font(21), fill=0)
        y += 35


def draw_tube(d, tube, status):
    d.text((M, 496), "Tube · Finsbury Park", font=font(25, True), fill=0)
    if tube is None and status is None:
        d.text((M, 532), "Tube information unavailable", font=font(22), fill=110)
        return
    y, limit = 532, 755
    for line_id in TUBE_LINES:
        line_status = (status or {}).get(line_id)
        name = line_status["name"] if line_status else line_id.title()
        d.text((M, y), name, font=font(24, True), fill=0)
        if line_status:
            rtext(d, W-M, y+2, line_status["status"], font(21, not line_status["ok"]), 0)
        y += 30
        if line_status and not line_status["ok"] and line_status["reason"]:
            for line in wrap(d, line_status["reason"], font(16), W-2*M, 2):
                d.text((M, y), line, font=font(16), fill=70)
                y += 19
        if SHOW_TUBE_ARRIVALS and tube:
            for train in tube.get(line_id, []):
                if y+23 > limit: break
                mins = ", ".join("due" if minute == 0 else str(minute) for minute in train["mins"]) + " min"
                mins_font = font(20, True)
                left = fit(d, f"→ {train['dest']}", font(20), W-2*M-d.textlength(mins, font=mins_font)-16)
                d.text((M+8, y), left, font=font(20), fill=0)
                rtext(d, W-M, y, mins, mins_font, 0)
                y += 24
        y += 5
    if status:
        disrupted = sorted(item["name"] for key, item in status.items()
                           if key not in TUBE_LINES and not item["ok"])
        summary = "Other lines: good service" if not disrupted else "Disruption: " + ", ".join(disrupted)
        d.text((M, 770), fit(d, summary, font(16), W-2*M), font=font(16), fill=70)


def render(weather, events, tube, status, now):
    image = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(image)
    draw.text((M, 12), f"{now.strftime('%A')} {now.day} {now.strftime('%B')}", font=font(28, True), fill=0)
    rtext(draw, W-M, 20, f"upd {now.strftime('%H:%M')}", font(18), 110)
    draw_weather(draw, weather)
    rule(draw, 296)
    draw_calendar(draw, events, now)
    rule(draw, 484)
    draw_tube(draw, tube, status)
    return image


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
        weather, events, tube, status = demo_data(now)
    else:
        weather = safe(lambda: fetch_weather(now), "weather")
        events = safe(lambda: fetch_calendar(now), "calendar")
        tube = safe(fetch_tube, "tube arrivals")
        status = safe(fetch_status, "tube status")
    image = render(weather, events, tube, status, now)
    (OUT / "api").mkdir(parents=True, exist_ok=True)
    image.save(OUT / "dashboard.png", optimize=True)
    stamp = now.strftime("%Y%m%d%H%M")
    metadata = {"status": 0, "image_url": f"{public_base()}/dashboard.png?v={stamp}",
                "filename": f"dash-{stamp}.png",
                "refresh_rate": int(os.environ.get("REFRESH_SECONDS", "600")),
                "update_firmware": False, "firmware_url": None, "reset_firmware": False}
    for path in (OUT / "api" / "display", OUT / "display.json"):
        path.write_text(json.dumps(metadata))
    (OUT / "index.html").write_text(
        '<!doctype html><meta name="viewport" content="width=device-width">'
        '<img src="dashboard.png" style="max-width:100%;border:1px solid #888">')
    print("wrote", OUT / "dashboard.png")


if __name__ == "__main__":
    main()
