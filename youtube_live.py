# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
FUENTES_FILE = ROOT / "fuentes_youtube.json"
RESULTADO_FILE = ROOT / "resultado_facebook_bolivia.json"
LIVES_FILE = ROOT / "lives_bolivia.json"
ESTADO_FILE = ROOT / ".youtube_live_estado.json"

# A diferencia de Facebook, YouTube no requiere navegador: la página /live de un
# canal trae en el HTML servido por el propio YouTube un marcador confiable
# ("isLiveNow":true) cuando ese canal está transmitiendo en este momento. Por eso
# alcanza con un GET simple, sin Playwright/Chromium.

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ISLIVE_RE = re.compile(r'"isLiveNow":true')
TITLE_TAG_RE = re.compile(r"<title>([^<]{1,200})</title>")
VIDEOID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
# El <meta property="og:image"> de la página /live de un canal es la miniatura del
# video en vivo, no el logo del canal — pero es lo único disponible sin autenticarse
# ni pegarle a la API oficial, y sirve igual de bien como ícono en la lista.
OGIMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def cargar_estado() -> dict:
    try:
        data = json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"fuentes": {}}


def guardar_estado(estado: dict):
    ESTADO_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fuente_key(f):
    return f.get("url") or f.get("nombre") or "desconocida"


def debe_revisar(f, estado, cooldown_minutes):
    key = fuente_key(f)
    item = estado.get("fuentes", {}).get(key, {})
    ultima = parse_iso(item.get("ultima_revision"))
    if not ultima:
        return True
    return now_utc() - ultima >= timedelta(minutes=cooldown_minutes)


def marcar_revision(estado, f, resultado):
    key = fuente_key(f)
    estado.setdefault("fuentes", {})[key] = {
        "ultima_revision": now_iso(),
        "ultimo_estado": resultado.get("estado"),
        "ultimo_video_id": resultado.get("video_id"),
    }


def normalize(url):
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    p = urlparse(raw)
    host = p.netloc.lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        raise ValueError("No es una URL permitida de YouTube")

    path = p.path.rstrip("/")
    parts = [x for x in path.split("/") if x]
    trailing = {"live", "videos", "streams", "featured", "about", "community"}
    while parts and parts[-1].lower() in trailing:
        parts.pop()
    if not parts:
        raise ValueError("No pude obtener el canal desde la URL")

    ident = "/".join(parts)
    return {"id": ident, "base": f"https://www.youtube.com/{ident}"}


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "es-419,es;q=0.9,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.geturl(), body


def titulo_desde_html(html):
    m = TITLE_TAG_RE.search(html)
    if not m:
        return None
    t = m.group(1).strip()
    t = re.sub(r"\s*-\s*YouTube$", "", t).strip()
    return t or None


def inspect(norm):
    live_url = norm["base"] + "/live"
    try:
        final_url, body = fetch(live_url)
    except urllib.error.HTTPError as e:
        return {"estado": "error", "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"estado": "error", "error": f"{type(e).__name__}: {e}"}

    if "consent.youtube.com" in final_url:
        return {"estado": "error", "error": "Bloqueado por muro de consentimiento de YouTube"}

    icono = None
    m_icono = OGIMAGE_RE.search(body)
    if m_icono:
        icono = m_icono.group(1)

    en_vivo = bool(ISLIVE_RE.search(body))
    if not en_vivo:
        return {"estado": "offline", "ruta_detectada": live_url, "icono_url": icono}

    q = parse_qs(urlparse(final_url).query)
    video_id = (q.get("v") or [None])[0]
    if not video_id:
        m = VIDEOID_RE.search(body)
        video_id = m.group(1) if m else None

    return {
        "estado": "live",
        "video_id": video_id,
        "url_video": f"https://www.youtube.com/watch?v={video_id}" if video_id else final_url,
        "titulo": titulo_desde_html(body),
        "ruta_detectada": live_url,
        "icono_url": icono,
    }


def revisar(f):
    out = {
        "nombre": f.get("nombre"),
        "categoria": f.get("categoria"),
        "pais": f.get("pais"),
        "plataforma": "youtube",
        "url_fuente": f.get("url"),
        "tipo": "canal",
        "identificador": None,
        "icono_url": None,
        "en_vivo": False,
        "estado": "offline",
        "titulo": None,
        "video_id": None,
        "url_video": None,
        "confianza": None,
        "ruta_detectada": None,
        "motivos": [],
        "espectadores": None,
        "revisado_en": now_iso(),
        "error": None,
    }

    try:
        n = normalize(f["url"])
        out["identificador"] = n["id"]

        r = inspect(n)
        out["estado"] = r.get("estado", "offline")
        out["ruta_detectada"] = r.get("ruta_detectada")
        out["icono_url"] = r.get("icono_url")

        if r["estado"] == "live":
            out.update({
                "en_vivo": True,
                "titulo": r.get("titulo"),
                "video_id": r.get("video_id"),
                "url_video": r.get("url_video"),
                "motivos": ["isLiveNow"],
            })
        elif r["estado"] == "error":
            out["error"] = r.get("error")

    except Exception as e:
        out["estado"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"

    return out


def guardar(rows):
    RESULTADO_FILE.write_text(
        json.dumps({
            "schema_version": 2,
            "generado_en": now_iso(),
            "fuentes_revisadas": len(rows),
            "en_vivo": sum(1 for x in rows if x["en_vivo"]),
            "resultados": rows,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lives = [x for x in rows if x["en_vivo"]]
    LIVES_FILE.write_text(
        json.dumps({
            "generado_en": now_iso(),
            "cantidad": len(lives),
            "lives": lives,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def scan(args):
    fuentes = json.loads(FUENTES_FILE.read_text(encoding="utf-8"))

    if args.url:
        fuentes = [{"nombre": "Prueba manual", "url": args.url, "categoria": "manual"}]
    elif args.source:
        s = args.source.lower()
        fuentes = [
            x for x in fuentes
            if s in x["nombre"].lower() or s in x["url"].lower()
        ]

    if args.offset:
        fuentes = fuentes[args.offset:]

    if args.limit:
        fuentes = fuentes[:args.limit]

    if not fuentes:
        print("No se encontraron fuentes")
        return []

    estado = cargar_estado()

    if not args.force:
        originales = len(fuentes)
        fuentes = [f for f in fuentes if debe_revisar(f, estado, args.cooldown_minutes)]
        omitidas = originales - len(fuentes)
        if omitidas:
            print(f"Se omiten {omitidas} fuentes por cache/cooldown local.")

    if not fuentes:
        print("Todas las fuentes están dentro del cooldown. Nada que revisar.")
        return []

    print(f"Revisando {len(fuentes)} canales de YouTube (sin navegador, solo HTTP).")

    rows = []
    for i, f in enumerate(fuentes, 1):
        print(f"[{i}/{len(fuentes)}] {f['nombre']} -> {f['url']}")
        r = revisar(f)
        rows.append(r)
        marcar_revision(estado, f, r)
        guardar_estado(estado)

        if r["en_vivo"]:
            print("   🔴 EN VIVO", r.get("url_video") or "")
        else:
            print("   Estado:", r["estado"])

        guardar(rows)

        if i < len(fuentes):
            time.sleep(args.delay)

    print(f"Listo. Lives detectados: {sum(1 for x in rows if x['en_vivo'])}")
    print("JSON:", LIVES_FILE)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")
    ap.add_argument("--url")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0,
                    help="Salta las primeras N fuentes antes de aplicar --limit.")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="Pausa fija entre fuentes. Mínimo 1s.")
    ap.add_argument("--cooldown-minutes", type=int, default=10,
                    help="No revisar la misma fuente más seguido que este intervalo.")
    ap.add_argument("--force", action="store_true",
                    help="Ignorar cache local. Úsalo solo para pruebas manuales puntuales.")
    ap.add_argument("--out-suffix", default="",
                    help="Sufijo para resultado_facebook_bolivia<sufijo>.json y lives_bolivia<sufijo>.json "
                         "(para que grupos en paralelo no se pisen entre sí).")

    args = ap.parse_args()

    global RESULTADO_FILE, LIVES_FILE
    if args.out_suffix:
        RESULTADO_FILE = ROOT / f"resultado_facebook_bolivia{args.out_suffix}.json"
        LIVES_FILE = ROOT / f"lives_bolivia{args.out_suffix}.json"

    args.delay = max(1.0, args.delay)
    args.cooldown_minutes = max(5, args.cooldown_minutes)

    scan(args)


if __name__ == "__main__":
    main()
