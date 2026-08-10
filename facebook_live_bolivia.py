# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
FUENTES_FILE = ROOT / "fuentes.json"
RESULTADO_FILE = ROOT / "resultado_facebook_bolivia.json"
LIVES_FILE = ROOT / "lives_bolivia.json"
ESTADO_FILE = ROOT / ".facebook_live_estado.json"

# Detector conservador: no intenta ocultar Playwright, falsificar huellas,
# rotar IPs/proxies ni evadir controles de Facebook.
# La estrategia para reducir bloqueos es: menos solicitudes, cache, pausas,
# backoff y detenerse cuando Facebook indica bloqueo.

POS_FUERTE = (
    "está transmitiendo en vivo", "esta transmitiendo en vivo",
    "está en vivo ahora", "esta en vivo ahora", "en vivo ahora",
    "is live now", "is broadcasting live",
    "transmitindo ao vivo", "está ao vivo", "esta ao vivo", "ao vivo agora",
)
POS_DEBIL = (" en vivo ", "\nen vivo\n", "🔴 en vivo", " ao vivo ", "🔴 ao vivo")
PASADO = (
    "was live", "estuvo en vivo", "transmitió en vivo", "transmitio en vivo",
    "foi ao vivo", "estava ao vivo",
    "en vivo anteriores", "vivo anteriores", "anteriormente en vivo",
    "videos ao vivo anteriores", "vídeos em direto anteriores",
    "previous live videos", "past live videos",
)
BLOQUEO = (
    "you're temporarily blocked", "you’re temporarily blocked",
    "temporarily blocked", "temporalmente bloqueado"
)

ESPECTADORES_RE = re.compile(
    r"([\d]+(?:[.,]\d+)?)\s*(mil|k)?\s*(?:personas\s+)?(?:est[aá]n\s+)?"
    r"(?:viendo|watching|espectadores|viewers)",
    re.I,
)

VIDEO_RES = [
    re.compile(r"https?://(?:www\.|m\.)?facebook\.com/[^/?#]+/videos/(?:[^/?#]+/)?(\d+)", re.I),
    re.compile(r"https?://(?:www\.|m\.)?facebook\.com/(\d+)/videos/(?:[^/?#]+/)?(\d+)", re.I),
    re.compile(r"https?://(?:www\.|m\.)?facebook\.com/watch/\?v=(\d+)", re.I),
]


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
    return {"fuentes": {}, "global": {}}


def guardar_estado(estado: dict):
    ESTADO_FILE.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize(url):
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    p = urlparse(raw)
    host = p.netloc.lower()
    if host not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        raise ValueError("No es una URL permitida de Facebook")

    q = parse_qs(p.query)
    path = p.path.rstrip("/")

    if path.lower().endswith("/profile.php") or path.lower() == "/profile.php":
        fid = (q.get("id") or [""])[0]
        if not fid:
            raise ValueError("profile.php sin id")
        return {"tipo": "profile_id", "id": fid, "base": f"https://www.facebook.com/{fid}"}

    m = re.match(r"^/groups/([^/?#]+)", path, re.I)
    if m:
        gid = m.group(1)
        return {"tipo": "grupo", "id": gid, "base": f"https://www.facebook.com/groups/{gid}"}

    parts = [x for x in path.split("/") if x]
    if parts and parts[0].lower() == "p" and len(parts) >= 2:
        last = parts[-1]
        mid = re.search(r"(\d{8,})$", last)
        ident = mid.group(1) if mid else last
        return {"tipo": "pagina", "id": ident, "base": f"https://www.facebook.com/{ident}"}

    if parts:
        ident = parts[0]
        return {"tipo": "pagina", "id": ident, "base": f"https://www.facebook.com/{ident}"}

    raise ValueError("No pude obtener identificador")


def routes(norm):
    """Pocas rutas por fuente para reducir solicitudes."""
    b = norm["base"].rstrip("/")
    if norm["tipo"] == "grupo":
        return [b, b + "/media/videos/"]
    # /live/ primero; /videos/ como respaldo. Evitamos cuatro cargas por fuente.
    return [b + "/live/", b + "/videos/"]


def clean_url(href):
    if not href:
        return ""
    href = href.replace("&amp;", "&")
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    if "/watch/" in href:
        return href
    return href.split("?")[0]


def extraer_espectadores(text):
    """Best-effort: busca frases tipo '1.1 mil espectadores' o '350 viewers'.
    No siempre está presente en el HTML; devuelve None si no se encuentra."""
    if not text:
        return None
    m = ESPECTADORES_RE.search(text)
    if not m:
        return None
    try:
        numero = float(m.group(1).replace(",", "."))
        if m.group(2):
            numero *= 1000
        return int(numero)
    except (ValueError, TypeError):
        return None


def video_id(url):
    for r in VIDEO_RES:
        m = r.search(url or "")
        if m:
            return m.groups()[-1]
    return None


def contexto(anchor):
    best = ""
    for xp in (
        "xpath=ancestor::div[@role='article'][1]",
        "xpath=ancestor::div[7]",
        "xpath=ancestor::div[6]",
        "xpath=ancestor::div[5]",
    ):
        try:
            n = anchor.locator(xp)
            if n.count():
                t = n.first.inner_text(timeout=800)
                if len(t) > len(best):
                    best = t
                if len(best) > 250:
                    break
        except Exception:
            pass
    return best


def score_live(text, aria="", html_hint=False):
    t = " " + (text or "").lower() + " "
    a = (aria or "").lower()
    score = 0
    why = []

    for x in PASADO:
        if x in t:
            score -= 200
            why.append("pasado:" + x)

    for x in POS_FUERTE:
        if x in t:
            score += 100
            why.append("live:" + x)

    for x in POS_DEBIL:
        if x in t:
            score += 30
            why.append("debil:" + x)

    if any(x in a for x in ("live", "en vivo", "ao vivo")):
        score += 100
        why.append("aria_live")

    if html_hint:
        score += 80
        why.append("html_live")

    return score, why


def titulo(text):
    for line in [
        re.sub(r"\s+", " ", x).strip()
        for x in (text or "").splitlines()
        if x.strip()
    ]:
        low = line.lower()
        if low in {
            "en vivo", "ao vivo", "live", "me gusta", "comentar",
            "compartir", "like", "comment", "share",
            "videos en vivo anteriores", "vídeos en vivo anteriores",
            "previous live videos", "videos ao vivo anteriores",
        }:
            continue
        if 5 <= len(line) <= 180:
            return line
    return None


def cerrar_popups(page):
    for tx in (
        "Permitir todas las cookies", "Allow all cookies",
        "Aceptar todas las cookies", "Solo permitir cookies esenciales",
        "Only allow essential cookies", "Ahora no", "Not now", "Cerrar", "Close"
    ):
        try:
            loc = page.get_by_text(tx, exact=False)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=700)
                page.wait_for_timeout(250)
                return
        except Exception:
            pass


def inspect(page, route, timeout=30000):
    page.goto(route, wait_until="domcontentloaded", timeout=timeout)
    # Espera funcional corta para que cargue contenido dinámico; no simula humano.
    page.wait_for_timeout(2500)
    cerrar_popups(page)
    page.wait_for_timeout(400)

    try:
        body = page.locator("body").inner_text(timeout=1800)
    except Exception:
        body = ""

    low = body.lower()
    if any(x in low for x in BLOQUEO):
        return {"estado": "blocked", "route": route}

    try:
        html = page.content().lower()
    except Exception:
        html = ""

    html_hint = any(x in html for x in (
        '"is_live_streaming":true',
        '"islivestreaming":true',
        '"broadcast_status":"live"',
        '"broadcaststatus":"live"',
    ))

    cand = []
    cur = clean_url(page.url)
    vid = video_id(cur)

    if vid and "/videos/" in cur:
        sc, why = score_live(body, html_hint=html_hint)
        if route.rstrip("/").endswith("/live"):
            sc += 70
            why.append("redirect_live")
        cand.append({
            "score": sc,
            "url_video": cur,
            "video_id": vid,
            "titulo": titulo(body),
            "motivos": why,
            "espectadores": extraer_espectadores(body),
        })

    anchors = page.locator(
        'a[href*="/videos/"],a[href*="/watch/?v="],a[href*="/watch/"][href*="v="]'
    )

    # Límite menor para no recorrer cientos de elementos innecesariamente.
    for i in range(min(anchors.count(), 60)):
        a = anchors.nth(i)
        try:
            href = clean_url(a.get_attribute("href") or "")
            vid = video_id(href)
            if not vid:
                continue
            ctx = contexto(a)
            aria = a.get_attribute("aria-label") or ""
            # html_hint NO se aplica aquí: es una señal a nivel de página completa
            # (puede venir de cualquier módulo embebido) y no de este link puntual.
            sc, why = score_live(ctx, aria=aria, html_hint=False)
            cand.append({
                "score": sc,
                "url_video": href,
                "video_id": vid,
                "titulo": titulo(ctx),
                "motivos": why,
                "espectadores": extraer_espectadores(ctx),
            })
        except Exception:
            pass

    cand.sort(key=lambda x: x["score"], reverse=True)
    if cand and cand[0]["score"] >= 70:
        return {"estado": "live", "route": route, **cand[0]}

    return {
        "estado": "offline",
        "route": route,
        "mejor_score": cand[0]["score"] if cand else None,
    }


def revisar(page, f):
    out = {
        "nombre": f.get("nombre"),
        "categoria": f.get("categoria"),
        "url_fuente": f.get("url"),
        "tipo": None,
        "identificador": None,
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
        out["tipo"], out["identificador"] = n["tipo"], n["id"]

        for route in routes(n):
            try:
                r = inspect(page, route)
            except PlaywrightTimeoutError:
                continue
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"
                continue

            if r["estado"] == "blocked":
                out["estado"] = "blocked"
                out["error"] = "Facebook mostró bloqueo temporal"
                break

            if r["estado"] == "live":
                out.update({
                    "en_vivo": True,
                    "estado": "live",
                    "titulo": r.get("titulo"),
                    "video_id": r.get("video_id"),
                    "url_video": r.get("url_video"),
                    "confianza": r.get("score"),
                    "ruta_detectada": r.get("route"),
                    "motivos": r.get("motivos") or [],
                    "espectadores": r.get("espectadores"),
                })
                break

            # Pausa corta entre rutas de la MISMA fuente para no hacer ráfaga.
            time.sleep(1.5)

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


def fuente_key(f):
    return f.get("url") or f.get("nombre") or "desconocida"


def debe_revisar(f, estado, cooldown_minutes):
    key = fuente_key(f)
    item = estado.get("fuentes", {}).get(key, {})
    ultima = parse_iso(item.get("ultima_revision"))
    if not ultima:
        return True
    return now_utc() - ultima >= timedelta(minutes=cooldown_minutes)


def global_en_cooldown(estado):
    hasta = parse_iso(estado.get("global", {}).get("blocked_until"))
    return bool(hasta and now_utc() < hasta)


def marcar_revision(estado, f, resultado):
    key = fuente_key(f)
    estado.setdefault("fuentes", {})[key] = {
        "ultima_revision": now_iso(),
        "ultimo_estado": resultado.get("estado"),
        "ultimo_video_id": resultado.get("video_id"),
    }


def marcar_bloqueo_global(estado, horas=6):
    estado.setdefault("global", {})["blocked_until"] = (
        now_utc() + timedelta(hours=horas)
    ).isoformat()


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

    if args.limit:
        fuentes = fuentes[:args.limit]

    if not fuentes:
        print("No se encontraron fuentes")
        return []

    estado = cargar_estado()

    if global_en_cooldown(estado) and not args.force:
        print("Hay cooldown global por un bloqueo anterior. No se harán solicitudes ahora.")
        return []

    if not args.force:
        originales = len(fuentes)
        fuentes = [
            f for f in fuentes
            if debe_revisar(f, estado, args.cooldown_minutes)
        ]
        omitidas = originales - len(fuentes)
        if omitidas:
            print(f"Se omiten {omitidas} fuentes por cache/cooldown local.")

    if not fuentes:
        print("Todas las fuentes están dentro del cooldown. Nada que revisar.")
        return []

    print(f"Revisando {len(fuentes)} fuentes. Sin login. Sin extraer m3u8.")
    print("Modo conservador: límite de frecuencia, cache y backoff.")

    rows = []

    with sync_playwright() as p:
        # Sin flags de stealth y sin user-agent falso.
        browser = p.chromium.launch(
            headless=not args.visible,
            args=["--no-sandbox"],
        )
        ctx = browser.new_context(
            locale="es-419",
            viewport={"width": 1365, "height": 900},
        )
        page = ctx.new_page()

        for i, f in enumerate(fuentes, 1):
            print(f"[{i}/{len(fuentes)}] {f['nombre']} -> {f['url']}")
            r = revisar(page, f)
            rows.append(r)
            marcar_revision(estado, f, r)
            guardar_estado(estado)

            if r["en_vivo"]:
                print("   🔴 EN VIVO", r.get("url_video") or "")
            else:
                print("   Estado:", r["estado"])

            guardar(rows)

            if r["estado"] == "blocked":
                marcar_bloqueo_global(estado, horas=args.block_hours)
                guardar_estado(estado)
                print(
                    f"Facebook indicó bloqueo temporal; se detiene el ciclo y "
                    f"se aplica cooldown de {args.block_hours}h."
                )
                break

            if i < len(fuentes):
                # Pausa fija para control de carga, no para simular comportamiento humano.
                time.sleep(args.delay)

        browser.close()

    guardar(rows)
    print(f"Listo. Lives detectados: {sum(1 for x in rows if x['en_vivo'])}")
    print("JSON:", LIVES_FILE)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source")
    ap.add_argument("--url")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--watch", type=int, metavar="SEGUNDOS")

    # Compatibilidad: se conservan estos flags, pero se normalizan a una sola pausa fija.
    ap.add_argument("--delay-min", type=float, default=None)
    ap.add_argument("--delay-max", type=float, default=None)
    ap.add_argument("--delay", type=float, default=8.0,
                    help="Pausa fija entre fuentes. Mínimo 5s.")
    ap.add_argument("--cooldown-minutes", type=int, default=10,
                    help="No revisar la misma fuente más seguido que este intervalo.")
    ap.add_argument("--block-hours", type=int, default=6,
                    help="Cooldown global si Facebook muestra bloqueo temporal.")
    ap.add_argument("--force", action="store_true",
                    help="Ignorar cache local. Úsalo solo para pruebas manuales puntuales.")

    args = ap.parse_args()

    # Si el workflow viejo pasa --delay-min/--delay-max, tomamos el mayor para ser conservadores.
    compat_delays = [x for x in (args.delay_min, args.delay_max) if x is not None]
    if compat_delays:
        args.delay = max([args.delay] + compat_delays)

    args.delay = max(5.0, args.delay)
    args.cooldown_minutes = max(5, args.cooldown_minutes)
    args.block_hours = max(1, args.block_hours)

    if args.watch:
        # Evita ciclos demasiado frecuentes cuando se usa como daemon local.
        sec = max(900, args.watch)
        try:
            while True:
                scan(args)
                print(f"Esperando {sec}s...")
                time.sleep(sec)
        except KeyboardInterrupt:
            print("Detenido.")
    else:
        scan(args)


if __name__ == "__main__":
    main()
