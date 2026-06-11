#!/usr/bin/env python3
"""Genera el EPG de StarTV (formato XMLTV) a partir de channels_resumen.json.

Pensado para correr en GitHub Actions, pero funciona igual en local.

Configuracion por variables de entorno:
  STARTV_TOKEN     (obligatorio)  Token JWT. Acepta con o sin prefijo "Bearer ".
  STARTV_APP_ID    (opcional)     Default: d47a651b-3842-46b1-9f2f-ac978a254b88
  STARTV_LINEUP_ID (opcional)     Default: 2342
  EPG_DAYS         (opcional)     Dias de programacion a pedir. Default: 7
  EPG_OUTPUT       (opcional)     Ruta del XML de salida. Default: public/epg.xml
  EPG_CHANNELS     (opcional)     Ruta del JSON de canales. Default: channels_resumen.json
  EPG_WORKERS      (opcional)     Descargas en paralelo. Default: 6
  EPG_PROXY        (opcional)     Proxy (ej: socks5://host:1080 o http://host:8080). Vacio = sin proxy.
"""

import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape as xml_escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Zona horaria de Mexico (UTC-6, sin horario de verano desde 2022).
TZ = timezone(timedelta(hours=-6))

APP_ID = os.environ.get("STARTV_APP_ID", "d47a651b-3842-46b1-9f2f-ac978a254b88")
LINEUP_ID = os.environ.get("STARTV_LINEUP_ID", "2342")
DAYS = int(os.environ.get("EPG_DAYS", "7"))
OUTPUT = os.environ.get("EPG_OUTPUT", "public/epg.xml")
CHANNELS_FILE = os.environ.get("EPG_CHANNELS", "channels_resumen.json")
WORKERS = int(os.environ.get("EPG_WORKERS", "6"))
PROXY = os.environ.get("EPG_PROXY", "").strip()
PAGE_SIZE = 5000

BASE = "https://edgelb.stargroup.com.mx:9443/xtv-ws-client/api/epgcache/list"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Content-Type": "application/json",
    "Origin": "https://edgelb.stargroup.com.mx",
    "Pragma": "no-cache",
    "Referer": "https://edgelb.stargroup.com.mx/web/startv/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}


def log(msg):
    print(msg, flush=True)


def normalize_token(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw
    return "Bearer " + raw


def token_expiry(token):
    """Devuelve la fecha de expiracion del JWT, o None si no se puede leer."""
    try:
        import base64

        payload_b64 = token.replace("Bearer ", "").split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None
    except Exception:
        return None


def make_session(authorization):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers["Authorization"] = authorization
    if PROXY:
        s.proxies.update({"http": PROXY, "https": PROXY})
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def xmltv_time(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=TZ).strftime("%Y%m%d%H%M%S %z")


def fetch_channel(session, ch, date_from, date_to):
    """Descarga los programas de un canal. Devuelve (channel, programs, error)."""
    channel_id = str(ch.get("contentId", ""))
    if not channel_id:
        return ch, [], "sin contentId"

    url = (
        f"{BASE}/{APP_ID}/{channel_id}/{LINEUP_ID}"
        f"?page=0&size={PAGE_SIZE}&dateFrom={date_from}&dateTo={date_to}"
    )
    try:
        r = session.get(url, timeout=60)
    except requests.RequestException as e:
        return ch, [], f"conexion: {e}"

    if r.status_code != 200:
        return ch, [], f"HTTP {r.status_code}"

    try:
        data = r.json()
    except ValueError:
        return ch, [], "JSON invalido"

    programs = (data.get("contents") or {}).get("content") or []
    return ch, programs, None


def build_channel_xml(ch):
    channel_id = xml_escape(str(ch.get("contentId", "")))
    name = ch.get("title", "") or ""
    number = ch.get("number", "")
    display = f"{number} {name}".strip()
    parts = [f'  <channel id="{channel_id}">']
    parts.append(f"    <display-name>{xml_escape(display)}</display-name>")
    parts.append(f"    <display-name>{xml_escape(name)}</display-name>")
    parts.append("  </channel>")
    return "\n".join(parts)


def build_programme_xml(channel_id, p):
    title = p.get("title") or ""
    start = p.get("startDateTime") or 0
    stop = p.get("endDateTime") or 0
    if not title or not start or not stop:
        return None

    cid = xml_escape(str(channel_id))
    parts = [
        f'  <programme start="{xmltv_time(start)}" stop="{xmltv_time(stop)}" channel="{cid}">',
        f'    <title lang="es">{xml_escape(title)}</title>',
    ]
    desc = p.get("description") or ""
    if desc:
        parts.append(f'    <desc lang="es">{xml_escape(desc)}</desc>')
    genre = p.get("genre") or ""
    if genre:
        parts.append(f'    <category lang="es">{xml_escape(genre)}</category>')
    parts.append("  </programme>")
    return "\n".join(parts)


def main():
    token = normalize_token(os.environ.get("STARTV_TOKEN", ""))
    if not token:
        log("ERROR: falta la variable STARTV_TOKEN")
        sys.exit(1)

    exp = token_expiry(token)
    if exp:
        now = datetime.now(timezone.utc)
        left = exp - now
        log(f"Token expira: {exp.isoformat()} (en {left})")
        if left.total_seconds() < 0:
            log("ERROR: el token ya esta caducado. Actualiza el secret STARTV_TOKEN.")
            sys.exit(2)
        if left.total_seconds() < 2 * 86400:
            log("AVISO: el token caduca en menos de 2 dias.")

    if not os.path.exists(CHANNELS_FILE):
        log(f"ERROR: no existe {CHANNELS_FILE}")
        sys.exit(1)

    with open(CHANNELS_FILE, encoding="utf-8") as f:
        channels = json.load(f)
    if not isinstance(channels, list):
        log("ERROR: channels_resumen.json no es una lista valida")
        sys.exit(1)

    channels = [c for c in channels if str(c.get("contentId", ""))]

    now = datetime.now(tz=TZ)
    start_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = (start_day + timedelta(days=DAYS)).replace(hour=23, minute=59, second=59)
    date_from = int(start_day.timestamp() * 1000)
    date_to = int(end_day.timestamp() * 1000)

    log(f"Canales: {len(channels)}")
    log(f"Rango EPG: {start_day} a {end_day} ({DAYS} dias)")
    log(f"Descargando con {WORKERS} workers...\n")

    session = make_session(token)

    channel_blocks = [build_channel_xml(c) for c in channels]
    programme_blocks = []
    total_programs = 0
    failed = []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(fetch_channel, session, c, date_from, date_to): c
            for c in channels
        }
        done = 0
        for fut in as_completed(futures):
            ch, programs, error = fut.result()
            done += 1
            cid = ch.get("contentId", "")
            name = ch.get("title", "")
            if error:
                log(f"[{done}/{len(channels)}] {cid} {name} -> ERROR: {error}")
                failed.append(f"{cid} {name} ({error})")
                continue
            count = 0
            for p in programs:
                block = build_programme_xml(cid, p)
                if block:
                    programme_blocks.append(block)
                    count += 1
            total_programs += count
            log(f"[{done}/{len(channels)}] {cid} {name} -> {count} programas")

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<tv generator-info-name="startv-epg">')
    xml.extend(channel_blocks)
    xml.extend(programme_blocks)
    xml.append("</tv>")
    xml_text = "\n".join(xml) + "\n"

    os.makedirs(os.path.dirname(OUTPUT) or ".", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(xml_text)
    with gzip.open(OUTPUT + ".gz", "wb") as f:
        f.write(xml_text.encode("utf-8"))

    log("")
    log(f"Listo. {OUTPUT} ({len(xml_text)} bytes) y {OUTPUT}.gz")
    log(f"Canales OK: {len(channels) - len(failed)}/{len(channels)}")
    log(f"Total programas: {total_programs}")
    if failed:
        log(f"Canales con error ({len(failed)}):")
        for f_ in failed:
            log(f"  - {f_}")

    # Si TODOS fallaron, lo mas probable es token caducado/invalido: fallar el job.
    if channels and len(failed) == len(channels):
        log("\nERROR: todos los canales fallaron. Revisa el token STARTV_TOKEN.")
        sys.exit(3)


if __name__ == "__main__":
    main()
