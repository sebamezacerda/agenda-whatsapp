"""Agenda por WhatsApp: resumen diario + recordatorio 1 hora antes de cada cita.

Se ejecuta cada ~10 min (GitHub Actions). Estado en sent.json (solo hashes).
Env:
  ICS_URLS         una por linea: "Etiqueta|https://...basic.ics" (o solo la URL)
  CALLMEBOT_PHONE  ej. +56912345678
  CALLMEBOT_APIKEY apikey entregada por CallMeBot
  DRY_RUN=1        imprime en vez de enviar
"""
import datetime as dt
import hashlib
import json
import os
import sys
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

TZ = ZoneInfo("America/Santiago")
STATE = Path(__file__).with_name("sent.json")
MORNING_HOUR, MORNING_MIN = 7, 30
REMIND_MINUTES = 60
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def h(s):
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"morning": [], "reminders": []}


def save_state(st):
    st["morning"] = st["morning"][-10:]
    st["reminders"] = st["reminders"][-500:]
    STATE.write_text(json.dumps(st))


def fetch_events(start, end):
    """Eventos [start, end) de todos los calendarios. Devuelve lista de dicts."""
    out = []
    sources = []
    for line in os.environ.get("ICS_URLS", "").splitlines():
        label, _, url = line.strip().rpartition("|")
        if url:
            sources.append((label or "Cal", url))
    for k, v in os.environ.items():  # un secret por calendario: ICS_PERSONAL, ICS_LOTUS...
        if k.startswith("ICS_") and k != "ICS_URLS" and v.strip():
            sources.append((k[4:].capitalize(), v.strip()))
    for label, url in sources:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
        for ev in recurring_ical_events.of(cal).between(start, end):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            s = ev.decoded("DTSTART")
            all_day = not isinstance(s, dt.datetime)
            if all_day:
                s = dt.datetime.combine(s, dt.time(0), TZ)
            elif s.tzinfo is None:
                s = s.replace(tzinfo=TZ)
            e = ev.decoded("DTEND", None)
            if isinstance(e, dt.datetime):
                e = e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)
            else:
                e = None
            out.append({
                "cal": label,
                "title": str(ev.get("SUMMARY", "(sin título)")),
                "start": s.astimezone(TZ),
                "end": e,
                "all_day": all_day,
                "where": str(ev.get("LOCATION", "") or "").strip(),
                "uid": str(ev.get("UID", "")) + s.isoformat(),
            })
    # dedupe (mismo evento visible en varios calendarios)
    seen, uniq = set(), []
    for e in sorted(out, key=lambda x: (not x["all_day"], x["start"], x["title"])):
        k = (e["title"], e["start"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def fmt_line(e):
    if e["all_day"]:
        t = "Todo el día"
    else:
        t = e["start"].strftime("%H:%M")
        if e["end"]:
            t += "–" + e["end"].strftime("%H:%M")
    line = f"• {t}  {e['title']} ({e['cal']})"
    if e["where"]:
        line += f"\n   📍 {e['where']}"
    return line


def send(text):
    if os.environ.get("DRY_RUN"):
        print("--- WHATSAPP ---\n" + text + "\n----------------")
        return
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": os.environ["CALLMEBOT_PHONE"],
                "apikey": os.environ["CALLMEBOT_APIKEY"], "text": text},
        timeout=30,
    )
    r.raise_for_status()


def main():
    now = dt.datetime.now(TZ)
    if os.environ.get("FAKE_NOW"):
        now = dt.datetime.fromisoformat(os.environ["FAKE_NOW"]).replace(tzinfo=TZ)
    st = load_state()
    changed = False
    today = now.date().isoformat()

    day0 = dt.datetime.combine(now.date(), dt.time(0), TZ)
    events = fetch_events(day0, day0 + dt.timedelta(days=1))

    # Resumen de la mañana (una vez al dia, desde las 7:30)
    if (now.hour, now.minute) >= (MORNING_HOUR, MORNING_MIN) and today not in st["morning"]:
        fecha = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]}"
        if events:
            body = "\n".join(fmt_line(e) for e in events)
            send(f"📅 *Agenda de hoy — {fecha}*\n\n{body}")
        else:
            send(f"📅 *{fecha}*\n\nHoy no tienes reuniones.")
        st["morning"].append(today)
        changed = True

    # Recordatorios: citas con hora que empiezan en <= 60 min
    for e in events:
        if e["all_day"]:
            continue
        mins = (e["start"] - now).total_seconds() / 60
        key = h(e["uid"])
        if 0 < mins <= REMIND_MINUTES and key not in st["reminders"]:
            send(f"⏰ *En {round(mins)} min* — {e['start'].strftime('%H:%M')}\n{fmt_line(e)}")
            st["reminders"].append(key)
            changed = True

    if changed and not os.environ.get("DRY_RUN"):
        save_state(st)


if __name__ == "__main__":
    sys.exit(main())
