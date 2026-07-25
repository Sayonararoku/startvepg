#!/usr/bin/env python3
"""Genera el EPG de StarTV (formato XMLTV) a partir de channels_resumen.json.

Pensado para correr en GitHub Actions, pero funciona igual en local.

TODO es automatico: el script consigue solo el token de sesion Y el appId (que
caduca cada ~dia). No hay que capturar ni renovar nada a mano. La cadena es:
  settings.json (deviceToken anonimo) -> guest-session (token fresco)
  -> login/cache/token (appId "uuid" + cacheUrl) -> epgcache/list.

Configuracion por variables de entorno (todas OPCIONALES):
  STARTV_TOKEN     Token JWT manual. Si se omite (o esta caducado), se genera solo.
  STARTV_APP_ID    appId manual. Si se omite, se pide fresco en cada corrida.
  STARTV_LINEUP_ID Default: 2342
  EPG_DAYS         Dias de programacion a pedir. Default: 7
  EPG_OUTPUT       Ruta del XML de salida. Default: public/epg.xml
  EPG_CHANNELS     Ruta del JSON de canales. Default: channels_resumen.json
  EPG_WORKERS      Descargas en paralelo. Default: 6
  EPG_PROXY        Proxy (ej: socks5://host:1080). Vacio = sin proxy.
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

# El guest-session/settings usan certis que conviene no verificar; silenciamos el aviso.
requests.packages.urllib3.disable_warnings()

# Zona horaria de Mexico (UTC-6, sin horario de verano desde 2022).
TZ = timezone(timedelta(hours=-6))

# appId manual opcional. Si se omite, se pide fresco (recomendado: dejarlo vacio).
MANUAL_APP_ID = (os.environ.get("STARTV_APP_ID") or "").strip()
LINEUP_ID = os.environ.get("STARTV_LINEUP_ID") or "2342"
DAYS = int(os.environ.get("EPG_DAYS") or "7")
OUTPUT = os.environ.get("EPG_OUTPUT") or "public/epg.xml"
CHANNELS_FILE = os.environ.get("EPG_CHANNELS") or "channels_resumen.json"
WORKERS = int(os.environ.get("EPG_WORKERS") or "6")
PROXY = os.environ.get("EPG_PROXY", "").strip()
PAGE_SIZE = 5000

# Endpoints para la sesion automatica.
SETTINGS_URL = "https://edgelb.stargroup.com.mx/web/startv/settings.json"
AUTH_BASE = "https://edgelb.stargroup.com.mx:4446/xtv-ws-client/api"
GUEST_SESSION_URL = AUTH_BASE + "/v1/guest-session"
CACHE_TOKEN_URL = AUTH_BASE + "/login/cache/token"

# Base del caché EPG (por si login/cache/token no trae cacheUrl).
CACHE_BASE_DEFAULT = "https://edgelb.stargroup.com.mx:9443/xtv-ws-client"

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


def _auto_client():
    sess = requests.Session()
    sess.headers.update(HEADERS)
    if PROXY:
        sess.proxies.update({"http": PROXY, "https": PROXY})
    return sess


def auto_session():
    """Cadena completa sin intervencion humana.

    settings.json -> deviceToken anonimo (valido por anios)
      -> guest-session -> token JWT fresco (en el header Authorization)
      -> login/cache/token -> appId ("uuid") + cacheUrl (base del epgcache).
    Devuelve (token, appid, cache_base). Cualquier campo puede venir vacio si falla.
    """
    token, appid, cache_base = "", "", ""
    try:
        sess = _auto_client()
        anon = sess.get(SETTINGS_URL, timeout=30, verify=False).json()
        anon = anon["anonymous-browsing"]["deviceToken"]
        gs = sess.get(
            GUEST_SESSION_URL, params={"deviceToken": anon}, timeout=30, verify=False
        )
        token = normalize_token(
            gs.headers.get("Authorization") or gs.headers.get("authorization") or ""
        )
        if not token:
            log("AVISO: guest-session no devolvio token.")
            return token, appid, cache_base
        ct = sess.get(
            CACHE_TOKEN_URL,
            params={"timestamp": int(time.time() * 1000)},
            headers={"Authorization": token},
            timeout=30,
            verify=False,
        ).json()
        tok = ct.get("token") or {}
        appid = (tok.get("uuid") or "").strip()
        cache_base = (tok.get("cacheUrl") or "").strip()
    except Exception as e:
        log(f"AVISO: fallo la sesion automatica: {e}")
    return token, appid, cache_base


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


def fetch_channel(session, ch, date_from, date_to, base, app_id):
    """Descarga los programas de un canal. Devuelve (channel, programs, error)."""
    channel_id = str(ch.get("contentId", ""))
    if not channel_id:
        return ch, [], "sin contentId"

    url = (
        f"{base}/api/epgcache/list/{app_id}/{channel_id}/{LINEUP_ID}"
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
    # Token manual (si esta vigente) y appId manual, ambos opcionales.
    manual_token = normalize_token(os.environ.get("STARTV_TOKEN", ""))
    if manual_token:
        mexp = token_expiry(manual_token)
        if mexp and mexp <= datetime.now(timezone.utc):
            log("STARTV_TOKEN manual esta caducado; se ignora.")
            manual_token = ""

    # SIEMPRE se prefiere la sesion automatica (token + appId frescos). Los valores
    # manuales (STARTV_TOKEN / STARTV_APP_ID) quedan solo como respaldo por si el auto
    # falla; asi un secret viejo (appId caducado) no rompe nada.
    log("Obteniendo sesion automatica (token + appId)...")
    a_token, a_appid, a_base = auto_session()
    token = a_token or manual_token
    appid = a_appid or MANUAL_APP_ID
    cache_base = a_base
    if not a_token and manual_token:
        log("Auto fallo; usando STARTV_TOKEN manual de respaldo.")
    if not a_appid and MANUAL_APP_ID:
        log("Auto fallo; usando STARTV_APP_ID manual de respaldo.")

    if not token:
        log("ERROR: no se pudo obtener el token (auto y manual fallaron).")
        sys.exit(1)
    if not appid:
        log("ERROR: no se pudo obtener el appId (auto y manual fallaron).")
        sys.exit(1)

    base = (cache_base or CACHE_BASE_DEFAULT).rstrip("/")

    exp = token_expiry(token)
    if exp:
        log(f"Token expira: {exp.isoformat()} (se regenera solo en cada corrida)")

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

    log(f"appId: {appid}  lineupId: {LINEUP_ID}")
    log(f"cacheBase: {base}")
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
            pool.submit(fetch_channel, session, c, date_from, date_to, base, appid): c
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

    # Si TODOS fallaron, algo cambio en la API: fallar el job.
    if channels and len(failed) == len(channels):
        log("\nERROR: todos los canales fallaron. Revisa la sesion/appId.")
        sys.exit(3)


if __name__ == "__main__":
    main()
