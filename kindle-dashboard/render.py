#!/usr/bin/env python3
"""Render the 600x800 greyscale Finsbury Park Kindle dashboard.

Shows 12-hour weather, calendar and commute information for Finsbury Park and Archway. Calendar access uses the private GOOGLE_CALENDAR_ICS_URL
GitHub Actions secret; the secret URL is never written into generated files.

Run `python render.py --demo` to render sample data without network access.
"""
import json
import math
import os
import re
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
HOURS = 12
TUBE_LINES = ["victoria", "jubilee", "elizabeth", "northern"]
SHOW_TUBE_ARRIVALS = True
MAX_CALENDAR_EVENTS = 4
RAIL_FROM, RAIL_TO = "FPK", "MOG"
RAIL_API_BASE = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2/LDBWS/api/20220120"
ARRIVAL_STOPS = {
    "victoria": ("940GZZLUFPK", "Finsbury Park"),
    "jubilee": ("940GZZLUGPK", "Green Park"),
    "elizabeth": ("910GLIVST", "Liverpool Street"),
    "northern": ("940GZZLUACY", "Archway"),
}

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
    """Fetch a 12-hour local forecast plus practical clothing indicators."""
    data = get_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": LAT, "longitude": LON, "timezone": "Europe/London",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_gusts_10m",
        "forecast_days": 2})
    daily = data["daily"]
    hourly = data["hourly"]
    cutoff = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hours = []
    for values in zip(hourly["time"], hourly["temperature_2m"],
                      hourly["apparent_temperature"], hourly["precipitation_probability"],
                      hourly["precipitation"], hourly["weather_code"],
                      hourly["wind_speed_10m"], hourly["wind_gusts_10m"]):
        stamp, temp, feels, probability, rain_mm, code, wind, gust = values
        dt = datetime.fromisoformat(stamp)
        if dt >= cutoff and temp is not None:
            hours.append({
                "label": dt.strftime("%H"),
                "temp": round(temp),
                "feels": round(feels if feels is not None else temp),
                "rain": round(probability or 0),
                "rain_mm": round(rain_mm or 0, 1),
                "code": code or 0,
                "wind": round(wind or 0),
                "gust": round(gust or 0),
            })
        if len(hours) >= HOURS:
            break
    current = data["current"]
    rain_peak = daily["precipitation_probability_max"][0]
    morning = [h for h in hours if 6 <= int(h["label"]) <= 11]
    wettest = max(morning, key=lambda h: h["rain"], default=None)
    feels_min = min((h["feels"] for h in morning), default=round(current.get("apparent_temperature", current["temperature_2m"])))
    max_gust = max((h["gust"] for h in morning), default=round(current.get("wind_gusts_10m", 0)))
    return {
        "temp": round(current["temperature_2m"]),
        "feels": round(current.get("apparent_temperature", current["temperature_2m"])),
        "code": current["weather_code"],
        "hi": round(daily["temperature_2m_max"][0]),
        "lo": round(daily["temperature_2m_min"][0]),
        "rain": 0 if rain_peak is None else round(rain_peak),
        "wind": round(current.get("wind_speed_10m", 0)),
        "gust": round(current.get("wind_gusts_10m", 0)),
        "sunrise": daily.get("sunrise", [""])[0][-5:] if daily.get("sunrise") else "",
        "sunset": daily.get("sunset", [""])[0][-5:] if daily.get("sunset") else "",
        "morning_feels_min": feels_min,
        "morning_rain": wettest["rain"] if wettest else 0,
        "morning_rain_mm": max((h["rain_mm"] for h in morning), default=0),
        "morning_gust": max_gust,
        "tomorrow_hi": round(daily["temperature_2m_max"][1]) if len(daily["temperature_2m_max"]) > 1 else None,
        "tomorrow_lo": round(daily["temperature_2m_min"][1]) if len(daily["temperature_2m_min"]) > 1 else None,
        "tomorrow_rain": round(daily["precipitation_probability_max"][1]) if len(daily["precipitation_probability_max"]) > 1 and daily["precipitation_probability_max"][1] is not None else None,
        "next_two_hour_rain": max((h["rain"] for h in hours[:2]), default=0),
        "hours": hours,
    }


def fetch_environment(now):
    """Fetch European AQI, fine particulates and seasonal pollen for Finsbury Park."""
    hourly_vars = "european_aqi,pm2_5,pm10,grass_pollen,birch_pollen,alder_pollen,mugwort_pollen,olive_pollen,ragweed_pollen"
    data = get_json("https://air-quality-api.open-meteo.com/v1/air-quality", {
        "latitude": LAT, "longitude": LON, "timezone": "Europe/London",
        "hourly": hourly_vars,
        "current": "european_aqi,pm2_5,pm10,grass_pollen,birch_pollen,alder_pollen,mugwort_pollen,olive_pollen,ragweed_pollen",
        "forecast_days": 2,
    })
    current = data.get("current") or {}
    hourly = data.get("hourly") or {}
    # Some pollen species are legitimately null outside their forecast season.
    pollen_values = []
    for species in ("grass_pollen", "birch_pollen", "alder_pollen",
                    "mugwort_pollen", "olive_pollen", "ragweed_pollen"):
        value = current.get(species)
        if value is None:
            values = hourly.get(species) or []
            value = next((item for item in values if item is not None), None)
        if value is not None:
            pollen_values.append(float(value))
    aqi = current.get("european_aqi")
    pm25 = current.get("pm2_5")
    pm10 = current.get("pm10")
    if aqi is None and not pollen_values and pm25 is None:
        return None
    if aqi is None:
        aqi_label = "AQI n/a"
    elif aqi <= 20:
        aqi_label = "Good"
    elif aqi <= 40:
        aqi_label = "Fair"
    elif aqi <= 60:
        aqi_label = "Moderate"
    elif aqi <= 80:
        aqi_label = "Poor"
    elif aqi <= 100:
        aqi_label = "Very poor"
    else:
        aqi_label = "Extremely poor"
    if not pollen_values:
        pollen_label = "Seasonal pollen n/a"
    else:
        peak = max(pollen_values)
        # Indicative concentration bands, not a clinical/allergy threshold.
        pollen_label = "Low" if peak < 10 else "Moderate" if peak < 50 else "High"
    return {
        "aqi": round(aqi) if aqi is not None else None,
        "aqi_label": aqi_label,
        "pm25": round(pm25, 1) if pm25 is not None else None,
        "pm10": round(pm10, 1) if pm10 is not None else None,
        "pollen_label": pollen_label,
        "pollen_available": bool(pollen_values),
    }


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


def fetch_rail(now):
    """Fetch Finsbury Park departures filtered to Moorgate using the RDM LDBWS API."""
    api_key = os.environ.get("LDBWS_API_KEY")
    if not api_key:
        raise RuntimeError("LDBWS_API_KEY is not set")

    url = f"{RAIL_API_BASE}/GetDepartureBoard/{RAIL_FROM}"
    response = requests.get(
        url,
        params={
            "numRows": 10,
            "filterCrs": RAIL_TO,
            "filterType": "to",
            "timeOffset": 0,
            "timeWindow": 120,
        },
        headers={"x-apikey": api_key, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    board = response.json()
    services = board.get("trainServices") or []
    departures = []

    for service in services:
        scheduled = (service.get("std") or "").strip()
        estimate = (service.get("etd") or "").strip()
        if not scheduled:
            continue
        effective_time = estimate if re.fullmatch(r"\d{1,2}:\d{2}", estimate) else scheduled
        try:
            departure_dt = datetime.strptime(effective_time, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day, tzinfo=TZ
            )
        except ValueError:
            continue

        # If the board contains a service just after midnight, place it on tomorrow.
        if departure_dt < now - timedelta(hours=12):
            departure_dt += timedelta(days=1)

        minutes_until = int((departure_dt - now).total_seconds() // 60)
        # Keep the user's requested safety cutoff: departures within 8 minutes are hidden.
        if minutes_until <= 8:
            continue

        cancelled = bool(service.get("isCancelled")) or estimate.lower() == "cancelled"
        if cancelled:
            status = "Cancelled"
        elif estimate.lower() == "on time":
            status = "On time"
        elif estimate.lower() in ("delayed", "no report"):
            status = estimate.title()
        elif estimate:
            status = f"Exp {estimate}"
        else:
            status = "No estimate"

        if cancelled:
            continue

        if 20 <= minutes_until <= 25:
            relevance = "GOOD WINDOW"
            band = "ideal"
        elif 15 <= minutes_until <= 19:
            relevance = "TIGHT"
            band = "tight"
        else:
            relevance = ""
            band = "later"

        departures.append({
            "scheduled": scheduled,
            "display_time": effective_time,
            "estimate": estimate,
            "platform": service.get("platform") or "—",
            "status": status,
            "minutes": minutes_until,
            "relevance": relevance,
            "band": band,
            "destination": ", ".join(
                location.get("locationName", "")
                for location in (service.get("destination") or [])
                if location.get("locationName")
            ) or "Moorgate",
        })

    departures.sort(key=lambda item: item["minutes"])
    return departures[:4]


def fetch_route_arrivals():
    """Arrivals relevant to the partner's two Canary Wharf route options."""
    result = {}
    for line_id, (stop_id, _label) in ARRIVAL_STOPS.items():
        try:
            data = get_json(f"https://api.tfl.gov.uk/Line/{line_id}/Arrivals/{stop_id}", tfl_params())
            data.sort(key=lambda item: item.get("timeToStation", 10**9))
            if data:
                result[line_id] = {
                    "station": _label,
                    "dest": (data[0].get("destinationName") or data[0].get("towards") or "").replace(" Underground Station", ""),
                    "mins": [max(0, round(item.get("timeToStation", 0) / 60)) for item in data[:3]],
                }
            else:
                result[line_id] = None
        except Exception as exc:
            print(f"[warn] {line_id} arrivals failed: {exc}", file=sys.stderr)
            result[line_id] = None
    return result


def fetch_status():
    """Service status for Victoria, Jubilee, Elizabeth and Northern lines."""
    data = get_json("https://api.tfl.gov.uk/Line/victoria,jubilee,elizabeth,northern/Status", tfl_params())
    lines = {}
    for line in data:
        statuses = line.get("lineStatuses") or [{}]
        disrupted = [item for item in statuses if item.get("statusSeverity") != 10]
        status = min(disrupted, key=lambda item: item.get("statusSeverity", 99)) if disrupted else statuses[0]
        reason = (status.get("reason") or "").strip()
        if ": " in reason[:40]:
            reason = reason.split(": ", 1)[1]
        lines[line["id"]] = {"name": line.get("name", line["id"].title()),
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
    weather = {
        "temp": 9, "feels": 6, "code": 3, "hi": 11, "lo": 5, "rain": 75,
        "wind": 14, "gust": 25, "sunrise": "07:30", "sunset": "16:00",
        "morning_feels_min": 5, "morning_rain": 70, "morning_rain_mm": 1.2,
        "morning_gust": 24, "tomorrow_hi": 10, "tomorrow_lo": 4,
        "tomorrow_rain": 35, "next_two_hour_rain": 25,
        "hours": [{"label": f"{(now.hour+i+1)%24:02d}",
                   "temp": 9-i//4, "feels": 6-i//5, "rain": [15,25,55,70,40,20,10,5,5,0,0,0][i],
                   "rain_mm": [0,0.1,0.4,0.8,0.3,0.1,0,0,0,0,0,0][i],
                   "code": [3,3,61,63,61,2,1,0,0,0,0,0][i],
                   "wind": 14, "gust": 24} for i in range(12)]
    }
    events = [
        {"date": now.date(), "time": "15:30", "title": "Project catch-up", "all_day": False},
        {"date": now.date(), "time": "18:00", "title": "Dinner with friends", "all_day": False},
        {"date": now.date()+timedelta(days=1), "time": "All day", "title": "Annual leave", "all_day": True},
    ]
    route_arrivals = {
        "victoria": {"station": "Finsbury Park", "dest": "Green Park", "mins": [2, 5, 8]},
        "jubilee": {"station": "Green Park", "dest": "Canary Wharf", "mins": [4, 7, 10]},
        "elizabeth": {"station": "Liverpool Street", "dest": "Canary Wharf", "mins": [3, 6, 9]},
        "northern": {"station": "Archway", "dest": "Kennington", "mins": [1, 4, 7]},
    }
    status = {
        "victoria": {"name": "Victoria", "status": "Good Service", "ok": True, "reason": ""},
        "jubilee": {"name": "Jubilee", "status": "Good Service", "ok": True, "reason": ""},
        "elizabeth": {"name": "Elizabeth", "status": "Good Service", "ok": True, "reason": ""},
        "northern": {"name": "Northern", "status": "Good Service", "ok": True, "reason": ""},
    }
    rail = [
        {"scheduled": "08:32", "platform": "1", "status": "On time", "minutes": 22,
         "relevance": "GOOD WINDOW", "band": "ideal"},
        {"scheduled": "08:39", "platform": "2", "status": "On time", "minutes": 17,
         "relevance": "TIGHT", "band": "tight"},
        {"scheduled": "08:46", "platform": "1", "status": "Delayed", "minutes": 28,
         "relevance": "", "band": "later"},
    ]
    environment = {"aqi": 18, "aqi_label": "Good", "pm25": 4.2, "pm10": 9.5,
                   "pollen_label": "Seasonal pollen n/a", "pollen_available": False}
    return weather, events, route_arrivals, status, environment, rail


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
        d.text((M, 100), "Weather unavailable", font=font(24), fill=110)
        return
    label, icon = describe(weather["code"])
    draw_icon(d, icon, M, 53, 82)
    d.text((120, 43), f"{weather['temp']}°", font=font(65, True), fill=0)
    d.text((122, 112), label, font=font(21), fill=0)
    d.text((360, 55), f"Feels {weather.get('feels', weather['temp'])}°", font=font(21, True), fill=0)
    d.text((360, 83), f"High {weather['hi']}° / Low {weather['lo']}°", font=font(18), fill=0)
    d.text((360, 109), f"Rain today {weather['rain']}%", font=font(18), fill=0)

    # Actionable morning advice, based on actual hourly forecast values.
    advice = []
    if weather.get("morning_rain", 0) >= 45 or weather.get("morning_rain_mm", 0) >= 0.5:
        advice.append("WATERPROOF")
    if weather.get("morning_feels_min", 12) <= 7:
        advice.append("EXTRA LAYER")
    if weather.get("morning_gust", 0) >= 30:
        advice.append("WINDY")
    advice_text = "  ·  ".join(advice) if advice else "NO SPECIAL WEATHER GEAR"
    d.text((M, 145), fit(d, advice_text, font(17, True), W-2*M), font=font(17, True), fill=0)

    hours = weather.get("hours") or []
    if hours:
        colw = (W-2*M)/len(hours)
        for i, hour in enumerate(hours):
            cx = M+colw*i+colw/2
            _, weather_icon = describe(hour["code"])
            ctext(d, cx, 172, hour["label"], font(15, True), 70)
            draw_icon(d, weather_icon, cx-15, 191, 30)
            ctext(d, cx, 222, f"{hour['temp']}°", font(18, True))
            ctext(d, cx, 244, f"{hour['rain']}%", font(13), 70)
            if i:
                d.line([M+colw*i, 171, M+colw*i, 263], fill=205, width=1)
    footer = f"Wind {weather.get('wind', 0)} · Gusts {weather.get('gust', 0)} km/h"
    next_rain = weather.get("next_two_hour_rain")
    if next_rain is not None:
        footer += f" · Rain next 2h {next_rain}%"
    d.text((M, 266), fit(d, footer, font(13), W-2*M), font=font(13), fill=85)
    tomorrow = f"Tomorrow {weather.get('tomorrow_hi', '—')}°/{weather.get('tomorrow_lo', '—')}°"
    if weather.get("tomorrow_rain") is not None:
        tomorrow += f" · Rain {weather['tomorrow_rain']}%"
    if weather.get("sunrise") and weather.get("sunset"):
        tomorrow += f" · Sunrise {weather['sunrise']} Sunset {weather['sunset']}"
    d.text((M, 281), fit(d, tomorrow, font(12), W-2*M), font=font(12), fill=100)


def draw_calendar(d, events, now, top=306):
    """Draw a compact calendar list and return the y-coordinate after it."""
    d.text((M, top), "Calendar · Today & tomorrow", font=font(25, True), fill=0)
    y = top + 39
    if events is None:
        d.text((M, y), "Calendar unavailable", font=font(22), fill=110)
        return y + 34
    if not events:
        d.text((M, y), "No more events today or tomorrow", font=font(21), fill=100)
        return y + 34

    today = now.date()
    for event in events[:MAX_CALENDAR_EVENTS]:
        event_date = event["date"]
        if event.get("all_day"):
            day_label = "TODAY" if event_date == today else "TOMORROW"
            time_label = "All day"
        else:
            day_label = "TODAY" if event_date == today else "TOMORROW"
            time_label = event["time"]

        # A clear time column keeps event titles aligned and easy to scan.
        d.text((M, y), day_label, font=font(14, True), fill=100)
        d.text((M, y + 15), time_label, font=font(18, True), fill=0)
        title_x = 158
        title_font = font(21)
        title = fit(d, event["title"], title_font, W - M - title_x)
        d.text((title_x, y + 4), title, font=title_font, fill=0)
        y += 34
    return y


def draw_rail(d, departures, top):
    """Compact National Rail panel; 12–15 minute departures are highlighted."""
    d.text((M, top), "YOUR TRAIN · FINSBURY PARK → MOORGATE",
           font=font(17, True), fill=0)
    y = top + 22
    if departures is None:
        d.text((M, y), "National Rail departures unavailable", font=font(14), fill=110)
        return y + 18
    if not departures:
        d.text((M, y), "No suitable departures in the next 2 hours", font=font(14), fill=90)
        return y + 18

    for service in departures[:3]:
        left = f"{service.get('display_time', service['scheduled'])}  Plat {service['platform']}"
        detail = f"{service['minutes']} min · {service['status']}"
        if service["relevance"] == "GOOD WINDOW":
            detail = f"{service['minutes']} min · GOOD WINDOW"
            shade = 0
        elif service["relevance"] == "TIGHT":
            detail += " · tight"
            shade = 75
        else:
            shade = 85
        d.text((M + 2, y), fit(d, left, font(14, True), 225), font=font(14, True), fill=0)
        rtext(d, W-M, y, detail, font(13, service["band"] == "ideal"), shade)
        y += 21
    return y + 1


def draw_commute(d, arrivals, status, top):
    d.text((M, top), "COMMUTE", font=font(23, True), fill=0)
    y = top + 30
    lines = [
        ("victoria", "Finsbury Park"),
        ("jubilee", "Green Park"),
        ("elizabeth", "Liverpool Street"),
        ("northern", "Archway"),
    ]
    for line_id, station in lines:
        item = (status or {}).get(line_id)
        name = item.get("name", line_id.title()) if item else line_id.title()
        status_text = item.get("status", "Status unknown") if item else "Status unknown"
        good = item.get("ok", False) if item else False
        d.text((M, y), name, font=font(19, True), fill=0)
        rtext(d, W-M, y+1, status_text, font(15, not good), 0 if not good else 65)
        y += 16

        live = (arrivals or {}).get(line_id)
        if live and live.get("mins"):
            mins = ", ".join("due" if minute == 0 else str(minute)
                             for minute in live["mins"][:3]) + " min"
            detail = f"{station} · {live.get('dest') or 'Departures'}"
            detail = fit(d, detail, font(14), W-2*M-d.textlength(mins, font=font(14, True))-12)
            d.text((M+6, y), detail, font=font(14), fill=75)
            rtext(d, W-M, y, mins, font(14, True), 0)
        else:
            d.text((M+6, y), f"{station} · live arrivals unavailable", font=font(14), fill=110)
        y += 14

        if item and not good and item.get("reason"):
            reason_lines = wrap(d, item["reason"], font(13), W-2*M, 1)
            for reason in reason_lines:
                d.text((M+6, y), reason, font=font(13), fill=80)
                y += 15
        y += 1
    return y


def draw_environment(d, environment, weather, top):
    """Compact combined air-quality and seasonal-pollen panel."""
    d.text((M, top), "ENVIRONMENT", font=font(21, True), fill=0)
    y = top + 26
    if environment is None:
        d.text((M, y), "Air quality and pollen forecast unavailable", font=font(15), fill=110)
        y += 20
    else:
        aqi = environment.get("aqi")
        aqi_text = f"EU AQI {aqi}" if aqi is not None else "EU AQI n/a"
        pm_text = f"PM2.5 {environment['pm25']} μg/m³" if environment.get("pm25") is not None else "PM2.5 n/a"
        pollen = environment.get("pollen_label", "Pollen n/a")
        d.text((M, y), aqi_text, font=font(16, True), fill=0)
        d.text((165, y), environment.get("aqi_label", "AQI n/a"), font=font(15), fill=75)
        rtext(d, W-M, y, pm_text, font(14), 65)
        y += 21
        d.text((M, y), f"Pollen · {pollen}", font=font(15, True), fill=0)
        if not environment.get("pollen_available"):
            rtext(d, W-M, y, "Off-season / n/a", font(13), 110)
        else:
            rtext(d, W-M, y, "Forecast", font(13), 90)
        y += 20
    return y


def render(weather, events, route_arrivals, status, environment, rail, now):
    image = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(image)
    draw.text((M, 8), f"{now.strftime('%A')} {now.day} {now.strftime('%B')}",
              font=font(25, True), fill=0)
    rtext(draw, W-M, 15, f"upd {now.strftime('%H:%M')}", font(16), 110)
    draw_weather(draw, weather)
    rule(draw, 296)
    calendar_bottom = draw_calendar(draw, events, now, top=304)

    rail_top = max(410, calendar_bottom + 5)
    rule(draw, rail_top - 6)
    rail_bottom = draw_rail(draw, rail, rail_top)

    commute_top = rail_bottom + 4
    rule(draw, commute_top - 4)
    commute_bottom = draw_commute(draw, route_arrivals, status, commute_top)

    environment_top = commute_bottom + 3
    if environment_top < 755:
        rule(draw, environment_top - 3)
        draw_environment(draw, environment, weather or {}, environment_top)
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
        weather, events, route_arrivals, status, environment, rail = demo_data(now)
    else:
        weather = safe(lambda: fetch_weather(now), "weather")
        events = safe(lambda: fetch_calendar(now), "calendar")
        route_arrivals = safe(fetch_route_arrivals, "commute arrivals")
        status = safe(fetch_status, "commute line status")
        environment = safe(lambda: fetch_environment(now), "environment")
        rail = safe(lambda: fetch_rail(now), "National Rail")

    image = render(weather, events, route_arrivals, status, environment, rail, now)
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
