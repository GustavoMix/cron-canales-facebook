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
from urllib.parse import urlparse, parse_qs, quote

try:
    # El modo --merge no necesita navegador.
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception

ROOT = Path(__file__).resolve().parent
FUENTES_FILE = ROOT / "fuentes.json"
RESULTADO_FILE = ROOT / "resultado_facebook_bolivia.json"
LIVES_FILE = ROOT / "lives_bolivia.json"
ESTADO_FILE = ROOT / ".facebook_live_estado.json"
SALUD_FILE = ROOT / "salud_fuentes.json"
AUDITORIA_FILE = ROOT / "auditoria_deteccion.json"

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

# Formato compacto tipo "2.2K" o "1.8 mil" sin palabra clave alrededor (el que Facebook
# usa en el badge visual del ícono de ojito). Solo se usa sobre el aria-label del link,
# nunca sobre el texto completo de la página, para no matchear precios o teléfonos sueltos.
ESPECTADORES_COMPACTO_RE = re.compile(r"([\d]+(?:[.,]\d+)?)\s*(mil|k)\b", re.I)

# Respaldo de icono cuando Graph API no resuelve una foto real (perfiles personales,
# IDs que ya no existen): el og:image de la propia página, público incluso sin login.
OGIMAGE_RE = re.compile(r'property="og:image"\s+content="([^"]+)"', re.I)

# --- Auditoría de detección -------------------------------------------------
# Estos marcadores NO puntúan ni cambian el resultado: solo se registran para
# medir. La idea es usar las transmisiones ya confirmadas (las que hoy detecta
# aria_live) como referencia y ver qué otros marcadores las acompañan siempre.
# El que aparezca en casi todos los live y en casi ningún offline es candidato a
# convertirse en una segunda señal real, y así dejar de depender de una sola.
#
# No cuesta ni una solicitud extra: el HTML y el texto ya están descargados.
MARCADORES_CANDIDATOS = {
    "html_is_live": '"is_live"',
    "html_is_live_true": '"is_live":true',
    "html_live_status": '"live_status"',
    "html_video_status_live": 'video_status_live',
    "html_broadcast_id": '"broadcast_id"',
    "html_is_live_streaming": '"is_live_streaming"',
    "html_broadcast_status": '"broadcast_status"',
    "html_live_video": '"livevideo"',
    "html_is_currently_live": '"is_currently_live"',
    "texto_espectadores": "espectadores",
    "texto_personas_viendo": "personas están viendo",
    "texto_en_directo": "en directo",
    "texto_comento": "comentó",
}

# Un muro de login no es lo mismo que "no está transmitiendo": es "Facebook no
# me dejó mirar". Hoy los dos casos terminan como offline y se confunden entre
# sí, lo que esconde puntos ciegos (páginas reales donde nunca se ve un video).
MURO_LOGIN = (
    "inicia sesión en facebook", "iniciar sesión en facebook",
    "log in to facebook", "you must log in to continue",
    "debes iniciar sesión para continuar",
    "entrar no facebook",
)


def analizar_pagina(html, body):
    """Devuelve (marcadores presentes, ¿parece muro de login?).

    Puramente observacional: nada de esto altera el puntaje ni el estado. Solo
    alimenta auditoria_deteccion.json para poder decidir con datos, y no a ojo,
    qué señal conviene agregar al detector."""
    h = (html or "").lower()
    t = (body or "").lower()
    presentes = [
        nombre for nombre, aguja in MARCADORES_CANDIDATOS.items()
        if aguja in (t if nombre.startswith("texto_") else h)
    ]
    # El muro de login se reconoce por el texto y porque la página queda casi vacía.
    muro = any(x in t for x in MURO_LOGIN) or len(t.strip()) < 200
    return presentes, muro


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

    # "/pages/Nombre/12345/": el ID numérico real va en un segmento propio, no pegado
    # al nombre como en "/p/". Sin este caso, parts[0] ("pages") se usaba como si
    # fuera el identificador, apuntando a un ID que no existe.
    if parts and parts[0].lower() == "pages" and len(parts) >= 3:
        mid = re.search(r"^\d+$", parts[-1])
        if mid:
            ident = parts[-1]
            return {"tipo": "pagina", "id": ident, "base": f"https://www.facebook.com/{ident}"}

    if parts:
        ident = parts[0]
        return {"tipo": "pagina", "id": ident, "base": f"https://www.facebook.com/{ident}"}

    raise ValueError("No pude obtener identificador")


# Fragmento del nombre de archivo de la silueta genérica que Facebook devuelve en
# graph.facebook.com/{id}/picture cuando NO pudo resolver una foto real de forma
# anónima (perfil personal privado, id que ya no existe, etc.) — en vez de fallar,
# redirige a este placeholder como si fuera una foto válida.
SILUETA_GENERICA = "176159830277856_972693363922829312"

# Hay una segunda variante genérica: en vez del jpg de arriba, un .gif servido desde
# el CDN de assets estáticos de Facebook (rsrc.php), no del CDN de contenido subido
# (scontent*.fbcdn.net) donde viven las fotos reales. Se detectó con IDs de páginas
# tipo "/pages/Nombre/ID/" que Graph API no puede resolver a una foto de verdad.
CDN_ESTATICO_GENERICO = "static.xx.fbcdn.net"


def icono_valido(id_grafo):
    """Confirma que graph.facebook.com/{id}/picture resolvió una foto real, no
    alguno de los placeholders genéricos que Facebook redirige como si fueran una
    foto válida. Un solo GET liviano, se hace una vez por fuente."""
    try:
        req = urllib.request.Request(
            f"https://graph.facebook.com/{id_grafo}/picture?type=large",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            final_url = resp.geturl()
        if SILUETA_GENERICA in final_url or CDN_ESTATICO_GENERICO in final_url:
            return False
        return True
    except Exception:
        return False


# Último recurso cuando ni Graph API ni el og:image de la propia página dieron una
# foto real (perfil bloqueado por login, id sin foto, etc.): buscar el logo en
# DuckDuckGo Images. Es un truco no oficial (endpoint interno de DDG) y se bloquea
# con facilidad — bastó una ráfaga de ~5 consultas seguidas para recibir 403 en las
# pruebas. Por eso: reintentos cortos, y si se detecta bloqueo, se apaga el resto de
# la corrida (BUSQUEDA_IMAGEN_BLOQUEADA) para no insistir en cada fuente restante.
# La foto que devuelve es un best-effort por texto, no una confirmación de que es el
# logo correcto: puede fallar en traer la imagen equivocada para nombres ambiguos.
BUSQUEDA_IMAGEN_BLOQUEADA = False


def _resultados_duckduckgo(query):
    q = quote(query)
    req = urllib.request.Request(
        f"https://duckduckgo.com/?q={q}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        html = resp.read().decode("utf-8", "ignore")
    m = re.search(r"vqd=([\d-]+)", html)
    if not m:
        return []
    req2 = urllib.request.Request(
        f"https://duckduckgo.com/i.js?q={q}&vqd={m.group(1)}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req2, timeout=8) as resp:
        data = json.loads(resp.read())
    return [r.get("image") for r in (data.get("results") or []) if r.get("image")]


def _es_imagen_descargable(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return (resp.headers.get("Content-Type") or "").startswith("image/")
    except Exception:
        return False


def buscar_imagen_web(nombre_canal):
    """Devuelve una URL de imagen para el nombre dado, o None si no encontró nada
    descargable o DuckDuckGo bloqueó la consulta."""
    global BUSQUEDA_IMAGEN_BLOQUEADA
    if BUSQUEDA_IMAGEN_BLOQUEADA or not nombre_canal:
        return None
    try:
        candidatos = _resultados_duckduckgo(f"{nombre_canal} logo facebook")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            BUSQUEDA_IMAGEN_BLOQUEADA = True
        return None
    except Exception:
        return None

    for url in candidatos[:5]:
        host = urlparse(url).netloc
        # Se descartan los resultados alojados en Facebook: son links a fotos que
        # requieren login (page HTML, no la imagen cruda), no sirven como <img src>.
        if "facebook.com" in host or "fbsbx.com" in host:
            continue
        if _es_imagen_descargable(url):
            return url
    return None


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


def _parsear_numero(valor, tiene_sufijo):
    try:
        numero = float(valor.replace(",", "."))
        if tiene_sufijo:
            numero *= 1000
        return int(numero)
    except (ValueError, TypeError):
        return None


def extraer_espectadores(text, aria=None):
    """Best-effort: busca frases tipo '1.1 mil espectadores' o '350 viewers' en el texto,
    y como respaldo el formato compacto '2.2K'/'1.8 mil' dentro del aria-label del link
    (donde Facebook suele poner esa cifra sin palabra clave alrededor).
    No siempre está presente; devuelve None si no se encuentra."""
    if text:
        m = ESPECTADORES_RE.search(text)
        if m:
            resultado = _parsear_numero(m.group(1), bool(m.group(2)))
            if resultado is not None:
                return resultado
    if aria:
        m = ESPECTADORES_COMPACTO_RE.search(aria)
        if m:
            return _parsear_numero(m.group(1), True)
    return None


def intentar_espectadores_movil(page, url_video):
    """Segundo intento, solo para fuentes ya confirmadas en vivo sin espectadores:
    m.facebook.com a veces expone ese dato como texto plano donde la versión de
    escritorio (www) no lo hace. Una visita extra nada más, no por cada fuente."""
    if not url_video:
        return None
    m_url = re.sub(r"^https?://(?:www\.)?facebook\.com", "https://m.facebook.com", url_video)
    if m_url == url_video:
        return None
    try:
        page.goto(m_url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(1500)
        cerrar_popups(page)
        body = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return None

    valor = extraer_espectadores(body)
    if valor is not None:
        return valor
    # Respaldo: formato compacto "2.2K"/"1.8 mil" directo en el texto de la página
    # móvil, sin palabra clave alrededor (igual que se hace con el aria-label en www).
    m = ESPECTADORES_COMPACTO_RE.search(body)
    if m:
        return _parsear_numero(m.group(1), True)
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

    # \b para no confundir "live" dentro de "delivery", "alive", "livestream", etc.
    if re.search(r"\blive\b", a) or "en vivo" in a or "ao vivo" in a:
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
        html_raw = page.content()
    except Exception:
        html_raw = ""
    html = html_raw.lower()

    html_hint = any(x in html for x in (
        '"is_live_streaming":true',
        '"islivestreaming":true',
        '"broadcast_status":"live"',
        '"broadcaststatus":"live"',
    ))

    # Observación para la auditoría (no influye en el puntaje ni en el estado).
    marcadores, muro_login = analizar_pagina(html_raw, body)

    # Respaldo de icono: el og:image de la propia página (funciona incluso para
    # perfiles personales, sin necesidad de Graph API ni token). Se guarda siempre
    # que se encuentre; revisar() solo lo usa si Graph API no dio una foto válida.
    og_icono = None
    m_og = OGIMAGE_RE.search(html_raw)
    if m_og:
        og_icono = m_og.group(1).replace("&amp;", "&")

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
                "espectadores": extraer_espectadores(ctx, aria=aria),
            })
        except Exception:
            pass

    cand.sort(key=lambda x: x["score"], reverse=True)
    obs = {"marcadores": marcadores, "muro_login": muro_login}
    if cand and cand[0]["score"] >= 70:
        return {"estado": "live", "route": route, "og_icono": og_icono, **obs, **cand[0]}

    return {
        "estado": "offline",
        "route": route,
        "og_icono": og_icono,
        **obs,
        "mejor_score": cand[0]["score"] if cand else None,
        # Aunque no llegue al puntaje de "en vivo", puede ser la última transmisión de
        # la fuente (terminó hace poco, Facebook sigue sirviendo la grabación). Se
        # guarda para que la app pueda ofrecer "ver la última" en vez de dejarlo mudo.
        "mejor_candidato": cand[0] if cand else None,
    }


def revisar(page, f):
    out = {
        "nombre": f.get("nombre"),
        "categoria": f.get("categoria"),
        # Clasificación opcional ("institucional", "medio", ...). Es informativa: la app
        # puede ignorarla sin problema, la agrupación visible sigue siendo "categoria".
        "tipo_fuente": f.get("tipo_fuente"),
        "pais": f.get("pais"),
        "plataforma": "facebook",
        "url_fuente": f.get("url"),
        "tipo": None,
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
        # Observación para la auditoría; no afecta lo que ve la app.
        "marcadores": [],
        "muro_login": False,
        "revisado_en": now_iso(),
        "error": None,
    }

    try:
        n = normalize(f["url"])
        out["tipo"], out["identificador"] = n["tipo"], n["id"]
        # Foto de perfil pública vía Graph API: no necesita token para páginas públicas,
        # solo redirige (302) a la imagen real en el CDN de Facebook. No aplica a grupos.
        # Se valida que no sea la silueta genérica (perfiles personales privados, IDs mal
        # resueltos) — mejor dejarlo en None y que la app use la inicial como respaldo,
        # que mostrar una foto que no es de nadie.
        if n["tipo"] in ("pagina", "profile_id") and icono_valido(n["id"]):
            out["icono_url"] = f"https://graph.facebook.com/{n['id']}/picture?type=large"

        # Mejor candidato a "última transmisión" visto en cualquiera de las rutas,
        # por si ninguna llega a puntaje de "en vivo" pero sí hay algo reciente
        # que mostrar como repris en vez de dejar la fuente muda.
        mejor_repris = None
        # Respaldo de icono desde el og:image de la página, por si Graph API no dio
        # una foto válida (perfiles personales, IDs mal resueltos).
        icono_respaldo = None
        # Para distinguir "no está transmitiendo" de "Facebook no me dejó mirar":
        # solo cuenta como muro de login si TODAS las rutas quedaron tapadas.
        rutas_vistas = 0
        rutas_tapadas = 0

        for route in routes(n):
            try:
                r = inspect(page, route)
            except PlaywrightTimeoutError:
                continue
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {e}"
                continue

            if icono_respaldo is None and r.get("og_icono"):
                icono_respaldo = r["og_icono"]

            # Se acumulan los marcadores de todas las rutas visitadas: lo que
            # interesa es qué se vio de esta fuente, no en cuál de las dos páginas.
            rutas_vistas += 1
            if r.get("muro_login"):
                rutas_tapadas += 1
            for marcador in r.get("marcadores") or []:
                if marcador not in out["marcadores"]:
                    out["marcadores"].append(marcador)

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
                if out["espectadores"] is None:
                    try:
                        out["espectadores"] = intentar_espectadores_movil(page, out["url_video"])
                    except Exception:
                        pass
                break

            candidato = r.get("mejor_candidato")
            if candidato and (mejor_repris is None or candidato["score"] > mejor_repris["score"]):
                mejor_repris = candidato

            # Pausa corta entre rutas de la MISMA fuente para no hacer ráfaga.
            time.sleep(1.5)

        # Nunca estuvo en vivo en esta corrida, pero hay un video reciente disponible:
        # se deja como "offline" (en_vivo sigue False) pero con url_video para que la
        # app pueda ofrecer "ver la última" en vez de un offline mudo.
        if not out["en_vivo"] and out["estado"] not in ("blocked", "error") and mejor_repris:
            out.update({
                "titulo": mejor_repris.get("titulo"),
                "video_id": mejor_repris.get("video_id"),
                "url_video": mejor_repris.get("url_video"),
                "confianza": mejor_repris.get("score"),
                "motivos": mejor_repris.get("motivos") or [],
            })

        if out["icono_url"] is None and icono_respaldo:
            out["icono_url"] = icono_respaldo

        # Último recurso, solo si Graph API y og:image fallaron los dos: buscar el
        # logo por nombre en DuckDuckGo. Ver buscar_imagen_web() — es best-effort,
        # no una foto confirmada de la fuente real.
        if out["icono_url"] is None:
            out["icono_url"] = buscar_imagen_web(out["nombre"])

        out["muro_login"] = rutas_vistas > 0 and rutas_tapadas == rutas_vistas

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


def rango_grupo(total, grupo, de):
    """Reparte `total` fuentes en `de` grupos parejos y devuelve (offset, limit) del
    grupo pedido (1-based). El resto se distribuye entre los primeros grupos, así que
    ningún grupo queda con más de una fuente extra respecto de otro.

    Esto reemplaza los offset/limit escritos a mano en cada workflow: al agregar
    fuentes a fuentes.json el reparto se reacomoda solo, sin dejar un grupo gigante
    (que se pasaba del timeout del job) ni fuentes sin revisar."""
    if de < 1:
        raise ValueError("--de debe ser 1 o más")
    if not 1 <= grupo <= de:
        raise ValueError(f"--grupo debe estar entre 1 y {de}")
    base, resto = divmod(total, de)
    offset = (grupo - 1) * base + min(grupo - 1, resto)
    limit = base + (1 if grupo <= resto else 0)
    return offset, limit


def cargar_salud() -> dict:
    try:
        data = json.loads(SALUD_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"fuentes": {}}


def fuente_respondio(r):
    """¿Facebook devolvió algo reconocible de esta fuente en esta corrida?

    Una página real, aunque esté offline, casi siempre deja al menos una pista:
    foto de perfil (Graph API u og:image) o algún link de video. Si no deja ninguna
    y tampoco dio error de red, lo más probable es que la URL no exista o ya no
    sea pública. Una sola corrida no alcanza para afirmarlo, por eso se cuentan
    fallas seguidas."""
    if r.get("estado") in ("live", "blocked"):
        return True
    return bool(r.get("icono_url") or r.get("video_id"))


def actualizar_salud(rows):
    """Lleva la cuenta, corrida a corrida, de qué fuentes responden y cuáles no.
    Sirve para detectar URLs muertas o mal escritas sin tener que revisarlas a mano."""
    salud = cargar_salud()
    fuentes = salud.setdefault("fuentes", {})

    for r in rows:
        key = r.get("url_fuente") or r.get("nombre")
        if not key:
            continue
        item = fuentes.setdefault(key, {
            "nombre": r.get("nombre"),
            "corridas": 0,
            "ok": 0,
            "fallas_seguidas": 0,
            "ultima_vez_ok": None,
        })
        # El merge corre al final de CADA grupo, así que vuelve a leer resultados de
        # grupos que no se re-escanearon. Sin este corte, una misma revisión se
        # contaría una vez por grupo e inflaría las estadísticas.
        if r.get("revisado_en") and r["revisado_en"] == item.get("ultimo_revisado_en"):
            continue
        item["ultimo_revisado_en"] = r.get("revisado_en")
        item["nombre"] = r.get("nombre") or item.get("nombre")
        item["corridas"] = item.get("corridas", 0) + 1
        item["ultimo_estado"] = r.get("estado")
        item["ultimo_error"] = r.get("error")
        if fuente_respondio(r):
            item["ok"] = item.get("ok", 0) + 1
            item["fallas_seguidas"] = 0
            item["ultima_vez_ok"] = now_iso()
        else:
            item["fallas_seguidas"] = item.get("fallas_seguidas", 0) + 1

    sospechosas = sorted(
        (
            {"url": k, **v}
            for k, v in fuentes.items()
            if v.get("fallas_seguidas", 0) >= 3
        ),
        key=lambda x: -x["fallas_seguidas"],
    )

    salud["generado_en"] = now_iso()
    salud["resumen"] = {
        "fuentes_seguidas": len(fuentes),
        "sospechosas": len(sospechosas),
        "nunca_respondieron": sum(1 for v in fuentes.values() if not v.get("ok")),
    }
    salud["sospechosas"] = sospechosas
    SALUD_FILE.write_text(
        json.dumps(salud, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if sospechosas:
        print(f"Salud: {len(sospechosas)} fuentes sin responder 3+ corridas seguidas "
              f"(ver {SALUD_FILE.name}; para quitarlas: --podar).")
    return salud


def actualizar_auditoria(rows):
    """Mide, corrida a corrida, qué tan sólido es el detector.

    El punto de partida es un problema real: hoy TODAS las transmisiones se
    detectan por una sola señal (aria_live). Las otras tres que tiene el código
    no dispararon nunca. Si Facebook cambia esa etiqueta, la detección cae a cero
    y todo aparece como "offline" sin que nadie se entere.

    Para poder agregar una segunda señal con fundamento, se usan las
    transmisiones ya confirmadas como referencia y se cuenta qué marcadores las
    acompañan. Un marcador que aparezca en casi todos los live y en casi ningún
    offline sirve; uno que aparezca en los dos por igual, no."""
    try:
        aud = json.loads(AUDITORIA_FILE.read_text(encoding="utf-8"))
        if not isinstance(aud, dict):
            raise ValueError
    except Exception:
        aud = {}

    tot = aud.setdefault("totales", {"live": 0, "offline": 0, "muro_login": 0})
    marc = aud.setdefault("marcadores", {})
    senales = aud.setdefault("senales_que_detectaron", {})
    vistos = aud.setdefault("_revisiones_contadas", {})

    for r in rows:
        key = r.get("url_fuente") or r.get("nombre")
        # Mismo corte que en salud: el merge relee grupos que no se re-escanearon.
        if not key or (r.get("revisado_en") and vistos.get(key) == r["revisado_en"]):
            continue
        vistos[key] = r.get("revisado_en")

        es_live = r.get("estado") == "live"
        if es_live:
            tot["live"] += 1
        elif r.get("estado") == "offline":
            tot["offline"] += 1
        else:
            continue

        if r.get("muro_login"):
            tot["muro_login"] += 1

        for m in r.get("marcadores") or []:
            fila = marc.setdefault(m, {"en_live": 0, "en_offline": 0})
            fila["en_live" if es_live else "en_offline"] += 1

        if es_live:
            for motivo in r.get("motivos") or []:
                # "live:está transmitiendo..." -> se agrupa por familia de señal.
                familia = motivo.split(":")[0]
                senales[familia] = senales.get(familia, 0) + 1

    # Ranking: qué marcador separa mejor live de offline.
    ranking = []
    for nombre, f in marc.items():
        cobertura = f["en_live"] / tot["live"] if tot["live"] else 0
        ruido = f["en_offline"] / tot["offline"] if tot["offline"] else 0
        ranking.append({
            "marcador": nombre,
            "cobertura_live": round(cobertura, 3),
            "ruido_offline": round(ruido, 3),
            "separacion": round(cobertura - ruido, 3),
            **f,
        })
    ranking.sort(key=lambda x: -x["separacion"])

    aud["generado_en"] = now_iso()
    aud["totales"] = tot
    aud["ranking_marcadores"] = ranking
    aud["lectura"] = (
        "cobertura_live alta + ruido_offline bajo = candidato a segunda señal. "
        "senales_que_detectaron muestra de qué sirve cada detector que YA existe: "
        "si una sola familia aparece siempre, el detector depende de un solo punto."
    )
    AUDITORIA_FILE.write_text(json.dumps(aud, ensure_ascii=False, indent=2), encoding="utf-8")

    if tot["muro_login"]:
        print(f"Auditoría: {tot['muro_login']} revisiones acumuladas terminaron en "
              f"muro de login (no son 'offline' de verdad).")
    return aud


def podar(min_fallas):
    """Saca de fuentes.json las fuentes que llevan N corridas seguidas sin responder.
    Es manual a propósito: nunca se ejecuta desde el cron."""
    salud = cargar_salud()
    muertas = {
        k for k, v in salud.get("fuentes", {}).items()
        if v.get("fallas_seguidas", 0) >= min_fallas and not v.get("ultima_vez_ok")
    }
    if not muertas:
        print(f"No hay fuentes con {min_fallas}+ fallas seguidas y ningún acierto previo. Nada que podar.")
        return

    fuentes = json.loads(FUENTES_FILE.read_text(encoding="utf-8"))
    quedan = [f for f in fuentes if f.get("url") not in muertas]
    for f in fuentes:
        if f.get("url") in muertas:
            print(f"Quitando: {f.get('nombre')} -> {f.get('url')}")

    FUENTES_FILE.write_text(
        json.dumps(quedan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Podadas {len(fuentes) - len(quedan)} fuentes. Quedan {len(quedan)}.")


def merge_grupos(sufijos):
    """Combina los resultado_facebook_bolivia<sufijo>.json de cada grupo en los
    archivos finales (sin sufijo). No escanea nada, solo lee lo que ya está
    en disco en este checkout."""
    rows = []
    for suf in sufijos:
        f = ROOT / f"resultado_facebook_bolivia{suf}.json"
        if not f.exists():
            print(f"Aviso: no existe {f.name}, se omite ese grupo.")
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            rows.extend(data.get("resultados", []))
        except Exception as e:
            print(f"Aviso: no se pudo leer {f.name}: {e}")

    guardar(rows)
    actualizar_salud(rows)
    actualizar_auditoria(rows)

    # Aviso de cobertura: si el combinado tiene menos fuentes que fuentes.json, algún
    # grupo no publicó todavía (normal la primera corrida tras cambiar el reparto) o
    # su job falló. Antes esto pasaba en silencio.
    try:
        total = len(json.loads(FUENTES_FILE.read_text(encoding="utf-8")))
    except Exception:
        total = None
    if total is not None and len(rows) < total:
        print(f"Aviso: el combinado trae {len(rows)} de {total} fuentes; "
              f"faltan grupos por publicar sus resultados.")

    print(f"Merge listo: {len(rows)} fuentes combinadas de {len(sufijos)} grupos.")
    return rows


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
    if sync_playwright is None:
        raise RuntimeError("Playwright no está instalado. Instalalo con 'pip install -r requirements.txt' "
                            "y 'playwright install chromium' para poder escanear Facebook.")

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
    elif args.limit == 0:
        print("Aviso: --limit 0 no recorta nada (se revisa todo lo que quede después "
              "de --offset). Para repartir en grupos parejos usá --grupo N --de T.")

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
    ap.add_argument("--offset", type=int, default=0,
                    help="Salta las primeras N fuentes antes de aplicar --limit (para repartir la lista en grupos).")
    ap.add_argument("--grupo", type=int,
                    help="Número de grupo a revisar (1..N). Se usa junto con --de; calcula "
                         "solo el offset y el limit, sin tocar los workflows al agregar fuentes.")
    ap.add_argument("--de", type=int,
                    help="En cuántos grupos parejos se reparte fuentes.json.")
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
    ap.add_argument("--out-suffix", default="",
                    help="Sufijo para resultado_facebook_bolivia<sufijo>.json y lives_bolivia<sufijo>.json "
                         "(para que grupos en paralelo no se pisen entre sí).")
    ap.add_argument("--merge", metavar="SUFIJOS",
                    help="No escanea nada: combina resultado_facebook_bolivia<sufijo>.json de cada grupo "
                         "(sufijos separados por coma, ej. _grupo1,_grupo2) en los archivos finales sin sufijo.")
    ap.add_argument("--merge-de", type=int, metavar="N",
                    help="Como --merge, pero arma solo la lista _grupo1.._grupoN.")
    ap.add_argument("--podar", action="store_true",
                    help="Quita de fuentes.json las fuentes que nunca respondieron en varias "
                         "corridas seguidas (según salud_fuentes.json). Solo uso manual.")
    ap.add_argument("--min-fallas", type=int, default=6,
                    help="Cuántas corridas seguidas sin responder hacen falta para podar una fuente.")

    args = ap.parse_args()

    if args.podar:
        podar(max(2, args.min_fallas))
        return

    if args.merge_de:
        merge_grupos([f"_grupo{i}" for i in range(1, args.merge_de + 1)])
        return

    if args.merge:
        merge_grupos(args.merge.split(","))
        return

    if args.de:
        total = len(json.loads(FUENTES_FILE.read_text(encoding="utf-8")))
        args.offset, args.limit = rango_grupo(total, args.grupo or 1, args.de)
        print(f"Grupo {args.grupo or 1} de {args.de}: fuentes {args.offset + 1}-"
              f"{args.offset + args.limit} de {total}.")

    global RESULTADO_FILE, LIVES_FILE
    if args.out_suffix:
        RESULTADO_FILE = ROOT / f"resultado_facebook_bolivia{args.out_suffix}.json"
        LIVES_FILE = ROOT / f"lives_bolivia{args.out_suffix}.json"

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
