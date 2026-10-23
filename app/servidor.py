# -*- coding: utf-8 -*-
"""
servidor.py  ·  La app local
============================
Arranca un pequeño servidor web en tu propio ordenador (solo accesible desde él,
en 127.0.0.1) y abre el navegador. Tus datos nunca salen de la carpeta mis_datos;
a internet solo se sale para descargar precios.
"""

import base64
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from werkzeug.serving import make_server

from . import almacen, buscar, exportar, fiscal, importar, motor, periodicas, plantilla, red

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(RAIZ, "app", "web")
# Otra carpeta de datos, solo para pruebas o capturas: PATRIMONIO_DATOS=ruta
DATOS = os.environ.get("PATRIMONIO_DATOS") or os.path.join(RAIZ, "mis_datos")
DEMO = os.path.join(RAIZ, "demo", "cartera.json")
PUERTO = int(os.environ.get("PATRIMONIO_PUERTO") or 8765)
HORAS_PRECIOS = 6          # al arrancar, se actualizan si tienen más de esto
# Sube cuando cambia cómo se guardan los precios: obliga a descargarlos de nuevo al arrancar.
# 2: la v1.1.2 deja de guardar los precios mensuales que daba Yahoo con range=max.
# 3: se guardan los splits de Yahoo y se tira la cola sin ajustar que quedaba tras un split.
VERSION_CACHE = 3

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False   # respeta el orden de tipos y listas al mandarlos al navegador
cerrojo = threading.RLock()  # el motor no admite dos cálculos (ni dos escrituras) a la vez


# ---------------------------------------------------------------- seguridad
# Rumbo es una web en tu propio ordenador. Para que nada más que tú pueda usarla:
#  1. Solo escucha en 127.0.0.1 (nadie de tu red ni de internet puede conectarse).
#  2. Solo atiende si la dirección es 127.0.0.1 o localhost: una web con un dominio que
#     apunte a tu ordenador (DNS rebinding) no puede hacerse pasar por ella.
#  3. Llave de acceso: un número aleatorio en mis_datos/.acceso. El lanzador abre la app
#     con ella y el navegador la guarda en una cookie que solo manda a la propia app
#     (SameSite=Strict, HttpOnly). Sin la llave no se ve ni se cambia nada: ni otra web
#     abierta en tu navegador, ni otro programa, ni otro usuario de este ordenador.
#  4. Además, lo que llega de otra web (Sec-Fetch-Site, Origin) se rechaza y la página
#     prohíbe cargar código o enviar datos a ningún sitio que no sea ella (CSP).

HOSTS_LOCALES = {"127.0.0.1", "localhost", "[::1]"}
COOKIE = "rumbo_acceso"
LIMITE_SUBIDA = 25 * 1024 * 1024      # archivos para importar o copias: de sobra
app.config["MAX_CONTENT_LENGTH"] = LIMITE_SUBIDA
_LLAVES = {}

PAGINA_SIN_LLAVE = """<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Rumbo</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:14vh auto;max-width:540px;padding:0 22px;color:#1d1d1b;background:#f9f9f7}
h1{font-size:22px}code{background:#ecebe6;padding:1px 6px;border-radius:5px}</style></head><body>
<h1>Abre Rumbo con su lanzador</h1>
<p>Por seguridad, Rumbo solo se abre desde su lanzador: <code>Iniciar.bat</code> en Windows,
<code>Iniciar.command</code> en Mac o <code>Iniciar.sh</code> en Linux. Haz doble clic en él y se abrirá aquí.</p>
<p>Así, ninguna otra página ni otro programa puede ver ni cambiar tus datos.</p></body></html>"""


def token_acceso():
    """La llave de esta instalación (se crea la primera vez y se guarda en mis_datos/.acceso)."""
    ruta = os.path.join(DATOS, ".acceso")
    if ruta in _LLAVES:
        return _LLAVES[ruta]
    llave = None
    try:
        with open(ruta, encoding="utf-8") as f:
            llave = f.read().strip()
    except OSError:
        pass
    if not llave or not re.fullmatch(r"[A-Za-z0-9_-]{40,}", llave):
        llave = secrets.token_urlsafe(32)
        os.makedirs(DATOS, exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(llave)
    _LLAVES[ruta] = llave
    return llave


def url_con_llave(puerto):
    return f"http://127.0.0.1:{puerto}/?acceso={token_acceso()}"


def hosts_permitidos():
    extra = os.environ.get("PATRIMONIO_HOSTS_PERMITIDOS") or ""
    return HOSTS_LOCALES | {h.strip().lower() for h in extra.split(",") if h.strip()}


def escucha_en():
    """Dónde escucha el servidor: 127.0.0.1 (tu ordenador) salvo en Docker (0.0.0.0)."""
    return (os.environ.get("PATRIMONIO_HOST") or "127.0.0.1").strip()


def en_red():
    """True si Rumbo está abierta a la red de casa (Docker en un NAS, una Raspberry Pi...)."""
    return escucha_en() not in ("127.0.0.1", "localhost", "::1")


def en_docker():
    return os.environ.get("PATRIMONIO_DOCKER") == "1"


NOMBRES_DE_CASA = (".local", ".lan", ".home", ".internal", ".home.arpa", ".ts.net")


def host_valido(host):
    """¿Es esta una dirección por la que se puede entrar en Rumbo?
    En tu ordenador, solo 127.0.0.1 y localhost. Abierta a la red de casa (Docker), además
    las IP privadas (192.168…, 10…, Tailscale) y los nombres de la red local (nas, raspberrypi.local).
    Un dominio de internet nunca: así una web no puede hacerse pasar por Rumbo (DNS rebinding)."""
    h = _sin_puerto(host)
    if h in hosts_permitidos():
        return True
    if not en_red():
        return False
    ip = h.strip("[]")
    try:
        direccion = ipaddress.ip_address(ip)
        return direccion.is_private or direccion.is_loopback or direccion in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        pass
    return bool(re.fullmatch(r"[a-z0-9-]{1,63}", h)) or h.endswith(NOMBRES_DE_CASA)


def _sin_puerto(host):
    host = (host or "").strip().lower()
    if host.startswith("["):                     # [::1]:8765
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _origen_local(url):
    """True si la URL (Origin o Referer) es de esta misma app."""
    m = re.match(r"^https?://([^/]+)", url or "")
    return bool(m) and host_valido(m.group(1))


def _rechazo(texto):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, errores=[texto]), 403
    if request.path == "/":
        return Response(PAGINA_SIN_LLAVE, status=403, mimetype="text/html")
    return Response(texto, status=403, mimetype="text/plain")


@app.before_request
def solo_desde_rumbo():
    if not host_valido(request.host):
        return Response("Rumbo no responde en esta dirección. Si entras con un nombre propio, añádelo en "
                        "PATRIMONIO_HOSTS_PERMITIDOS.", status=403, mimetype="text/plain")
    if request.path == "/api/ping":
        return None   # solo dice «soy Rumbo»: el lanzador lo usa para no abrirla dos veces
    # Los navegadores modernos dicen quién hace la petición. Solo vale la propia app
    # («same-origin») o escribir la dirección / abrirla desde el lanzador («none»).
    sitio = request.headers.get("Sec-Fetch-Site")
    if sitio and sitio not in ("same-origin", "none"):
        abrir_app = (request.method == "GET" and request.path == "/"
                     and request.headers.get("Sec-Fetch-Mode") == "navigate")
        if not abrir_app:
            return _rechazo("Petición bloqueada: no viene de Rumbo.")
    # Navegadores sin Sec-Fetch: lo que cambia datos tiene que venir de esta app.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origen = request.headers.get("Origin") or request.headers.get("Referer")
        if origen and not _origen_local(origen):
            return _rechazo("Petición bloqueada: no viene de Rumbo.")
    # La llave: o llega en la dirección (al abrir desde el lanzador) o en la cookie.
    llave = token_acceso()
    dada = request.args.get("acceso")
    if dada is not None:
        if not hmac.compare_digest(dada, llave):
            return _rechazo("Llave de acceso incorrecta: abre Rumbo con su lanzador.")
        g.poner_cookie = True
        if request.method == "GET":
            # La llave no se queda en la barra de direcciones; lo demás (?tab=, ?novedades=) sí.
            resto = urllib.parse.urlencode([(k, v) for k, v in request.args.items(multi=True) if k != "acceso"])
            return redirect(request.path + ("?" + resto if resto else ""))
        return None
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), llave):
        return _rechazo("Sin llave de acceso: abre Rumbo con su lanzador.")
    return None


def _hashes_en_linea():
    """Huella de los <script> escritos dentro de index.html (el cargador): son los únicos
    scripts en línea que la página puede ejecutar."""
    try:
        with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return []
    huellas = set()
    for codigo in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
        for variante in (codigo, codigo.replace("\r\n", "\n")):   # el navegador quita los \r
            huellas.add("'sha256-" + base64.b64encode(hashlib.sha256(variante.encode("utf-8")).digest()).decode() + "'")
    return sorted(huellas)


CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' " + " ".join(_hashes_en_linea()),
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    # El precio del bitcoin en vivo lo pide el navegador (solo el nombre de la moneda).
    "connect-src 'self' https://api.coingecko.com https://api.binance.com",
    "font-src 'self'", "object-src 'none'", "base-uri 'none'", "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.before_request
def mismo_perfil_que_la_pagina():
    """El perfil activo es uno para todo Rumbo. Una pestaña abierta con otro perfil (porque se cambió
    en otra pestaña) no puede guardar: lo haría en el perfil equivocado. La página dice el suyo en la
    cabecera X-Rumbo-Perfil; si no coincide, no se toca nada y se le pide recargar."""
    pid = request.headers.get("X-Rumbo-Perfil")
    if not pid or request.method in ("GET", "HEAD", "OPTIONS") or request.path.startswith("/api/perfiles"):
        return None
    d = perfiles()
    if pid == d["activo"]:
        return None
    nombre = "Todos juntos" if d["activo"] == TODOS else next(
        (x["nombre"] for x in d["lista"] if x["id"] == d["activo"]), d["activo"])
    return jsonify(ok=False, perfilCambiado=True, errores=[
        f"En otra pestaña has cambiado al perfil «{nombre}». Recarga esta página para seguir: no se ha guardado nada."]), 409


@app.after_request
def cabeceras_seguras(resp):
    if g.get("poner_cookie"):
        resp.set_cookie(COOKIE, token_acceso(), max_age=400 * 24 * 3600, httponly=True,
                        samesite="Strict", path="/")
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = resp.headers.get("Cache-Control") or "no-store"
    return resp


# ---------------------------------------------------------------- archivos

def lee_json(ruta, defecto=None):
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defecto


def escribe_json(ruta, datos):
    """Escribe en un archivo temporal y lo renombra: si se corta, no se pierde nada."""
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


# ---------------------------------------------------------------- perfiles
# Cada perfil (tú, tu pareja, tus hijos…) es una cartera en su propia carpeta: el principal
# en mis_datos (como siempre) y los demás en mis_datos/perfiles/<id>. Los precios
# descargados se comparten. «Todos juntos» suma todos los perfiles, solo para mirar.

TODOS = "todos"
PRINCIPAL = "principal"


def perfiles():
    d = lee_json(os.path.join(DATOS, "perfiles.json"), {}) or {}
    lista = [x for x in d.get("lista") or [] if isinstance(x, dict) and isinstance(x.get("nombre"), str)
             and re.fullmatch(r"[a-z0-9_]{1,40}", str(x.get("id") or ""))]
    if not any(x["id"] == PRINCIPAL for x in lista):
        lista.insert(0, {"id": PRINCIPAL, "nombre": "Yo"})
    activo = d.get("activo") or PRINCIPAL
    if activo != TODOS and not any(x["id"] == activo for x in lista):
        activo = PRINCIPAL
    if activo == TODOS and len(con_cartera(lista)) < 2:
        activo = PRINCIPAL
    return {"activo": activo, "lista": lista}


def guarda_perfiles(d):
    escribe_json(os.path.join(DATOS, "perfiles.json"), {"activo": d["activo"], "lista": d["lista"]})


def carpeta(pid=None):
    """Carpeta de datos del perfil (por defecto, el activo)."""
    pid = pid or perfiles()["activo"]
    return DATOS if pid in (PRINCIPAL, TODOS) else os.path.join(DATOS, "perfiles", pid)


def con_cartera(lista):
    return [x for x in lista if os.path.exists(os.path.join(carpeta(x["id"]), "cartera.json"))]


def cartera_todos():
    """Una sola cartera con los productos de todos los perfiles, cada uno con su nombre detrás."""
    cfg = json.loads(json.dumps(almacen.CARTERA_VACIA))
    lista = con_cartera(perfiles()["lista"])
    cfg["titular"] = "Todos juntos"
    for per in lista:
        c = lee_json(os.path.join(carpeta(per["id"]), "cartera.json"), {}) or {}
        if per["id"] == PRINCIPAL:
            cfg["hitos"] = c.get("hitos") or cfg["hitos"]
            cfg["objetivo"] = c.get("objetivo") or cfg["objetivo"]
        pre = per["id"] + "__"
        for x in c.get("productos", []):
            corto = x.get("corto") or x.get("nombre") or x["id"]
            cfg["productos"].append({**x, "id": pre + x["id"], "nombre": f"{x.get('nombre') or corto} ({per['nombre']})",
                                     "corto": f"{corto[:18]} · {per['nombre'][:8]}", "perfil": per["nombre"]})
        for m in c.get("movimientos", []):
            nuevo = {**m, "id": pre + str(m.get("id")), "producto": pre + str(m.get("producto"))}
            if m.get("destino"):
                nuevo["destino"] = pre + str(m["destino"])
            cfg["movimientos"].append(nuevo)
        for v in c.get("valoraciones", []):
            cfg["valoraciones"].append({**v, "id": pre + str(v.get("id")), "producto": pre + str(v.get("producto"))})
    cfg["movimientos"].sort(key=lambda m: m["fecha"])
    return cfg


def modo():
    """'propio' si el perfil tiene ya su cartera; 'demo' si es el principal y aún no;
    'todos' en la vista que suma todos los perfiles."""
    if perfiles()["activo"] == TODOS:
        return "todos"
    return "propio" if os.path.exists(os.path.join(carpeta(), "cartera.json")) else "demo"


def cartera():
    m = modo()
    if m == "todos":
        return cartera_todos()
    return lee_json(os.path.join(carpeta(), "cartera.json") if m == "propio" else DEMO, {})


def ruta_calculado():
    return os.path.join(carpeta(), f"calculado_{modo()}.json")


def estado():
    return lee_json(os.path.join(DATOS, "estado.json"), {})


# ---------------------------------------------------------------- cálculo

def recalcula(descargar):
    """Recalcula el panel. Con descargar=True baja antes los precios nuevos;
    con "faltan", solo los de productos que aún no tienen precios guardados."""
    with cerrojo:
        datos = motor.construir(cartera(), carpeta(), descargar=descargar, cache=os.path.join(DATOS, "cache"),
                                historico=modo() != "todos")
        est = estado()
        if descargar is True:
            est["preciosActualizados"] = dt.datetime.now().replace(microsecond=0).isoformat()
            est["versionCache"] = VERSION_CACHE
            escribe_json(os.path.join(DATOS, "estado.json"), est)
        if datos is not None:
            datos["modo"] = modo()
            datos["preciosActualizados"] = est.get("preciosActualizados")
        escribe_json(ruta_calculado(), datos)
        return datos


def precios_viejos():
    ultima = estado().get("preciosActualizados")
    if not ultima or not os.path.exists(ruta_calculado()) or estado().get("versionCache") != VERSION_CACHE:
        return True
    return dt.datetime.now() - dt.datetime.fromisoformat(ultima) > dt.timedelta(hours=HORAS_PRECIOS)


# ---------------------------------------------------------------- rutas

@app.get("/")
def inicio():
    return send_from_directory(WEB, "index.html")


@app.get("/datos.js")
def datos_js():
    datos = lee_json(ruta_calculado())
    if datos is None and not os.path.exists(ruta_calculado()):
        datos = recalcula(descargar=False)
    elif modo() == "demo" and os.path.getmtime(DEMO) > os.path.getmtime(ruta_calculado()):
        datos = recalcula(descargar=False)   # la cartera de ejemplo cambió con una versión nueva
    cuerpo = ("window.DATOS = " + json.dumps(datos, ensure_ascii=False, separators=(",", ":")) + ";\n"
              + "window.PERFILES = " + json.dumps(info_perfiles(), ensure_ascii=False) + ";\n")
    return Response(cuerpo, mimetype="application/javascript",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/actualizar")
def api_actualizar():
    try:
        datos = recalcula(descargar=True)
    except Exception as e:  # que la app no se caiga nunca por un precio
        logging.exception("Fallo al actualizar")
        return jsonify(ok=False, error=f"No he podido actualizar: {e}"), 500
    return jsonify(ok=True, avisos=(datos or {}).get("avisos", []))


@app.get("/api/cartera")
def api_cartera():
    return jsonify(modo=modo(), cartera=cartera(), tipos=motor.TIPOS, fuentes=motor.FUENTES,
                   tiposMovimiento=almacen.TIPOS_MOV)


@app.get("/api/buscar")
def api_buscar():
    n = len(buscar.FALLOS)
    res = buscar.buscar(request.args.get("q", ""))
    return jsonify(resultados=res, sinConexion=(not res and bool(buscar.hubo_fallos_desde(n))))


GUARDAR = {"productos": almacen.guarda_producto, "movimientos": almacen.guarda_movimiento,
           "valoraciones": almacen.guarda_valoracion}
BORRAR = {"productos": almacen.borra_producto, "movimientos": almacen.borra_movimiento,
          "valoraciones": almacen.borra_valoracion}
AVISO_DEMO = ("Estás viendo la cartera de ejemplo. Pulsa «Empezar con mis datos» "
              "para crear la tuya y poder guardar cambios.")
AVISO_TODOS = ("Estás viendo «Todos juntos», que solo sirve para mirar. Elige un perfil arriba "
               "para cambiar sus datos.")


def bloqueado():
    """Respuesta de error si ahora no se pueden cambiar datos (demo o «Todos juntos»)."""
    m = modo()
    if m == "demo":
        return jsonify(ok=False, errores=[AVISO_DEMO]), 403
    if m == "todos":
        return jsonify(ok=False, errores=[AVISO_TODOS]), 403
    return None


def cambia(fn):
    """Aplica un cambio a la cartera, la guarda (con copia automática) y recalcula."""
    if bloqueado():
        return bloqueado()
    with cerrojo:
        ruta = os.path.join(carpeta(), "cartera.json")
        cfg = almacen.carga(ruta)
        try:
            item = fn(cfg)
        except almacen.ErrorValidacion as e:
            return jsonify(ok=False, errores=e.errores), 400
        almacen.guarda(ruta, cfg)
        datos = recalcula(descargar="faltan")
    completa_exposiciones()
    return jsonify(ok=True, item=item, cartera=cfg, avisos=(datos or {}).get("avisos", []))


_COMPLETANDO = threading.Lock()


def completa_exposiciones(esperar=False):
    """Descarga aparte el reparto por países y sectores de los productos que aún no lo
    tienen (Morningstar tarda unos segundos por fondo) y, si llega algo, recalcula.

    Qué falta y en qué carpeta se decide AHORA, no dentro del hilo: el hilo trabaja solo
    sobre esa carpeta, y si mientras tanto cambia (otra carpeta de datos), no recalcula."""
    from . import exposicion
    base = DATOS
    cache = os.path.join(base, "cache")

    def sin_consultar(p):
        k = exposicion.clave(p)
        return k and not p.get("exposicion") and not os.path.exists(
            os.path.join(cache, "EXPO_" + re.sub(r"[^A-Za-z0-9]", "_", k) + ".json"))
    try:
        faltan = [p for p in cartera().get("productos", []) if sin_consultar(p)]
    except Exception:
        return
    if not faltan:
        return

    def trabajo():
        if not _COMPLETANDO.acquire(blocking=False):
            return
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as ex:
                llegados = [r for r in ex.map(lambda p: exposicion.de_producto(p, cache, red=True), faltan) if r]
            if llegados and DATOS == base:
                recalcula(descargar=False)
        except Exception:
            logging.exception("No he podido completar el reparto por países")
        finally:
            _COMPLETANDO.release()
    if esperar:
        trabajo()
    else:
        threading.Thread(target=trabajo, daemon=True).start()


def cambio_del_dia(moneda, fecha_iso):
    """Euros por cada unidad de 'moneda' ese día (o el último día anterior con cambio)."""
    fx = motor.valor_en(motor.serie_cambio(moneda, DATOS), fecha_iso, margen=6)
    if not fx:   # el cambio guardado no llega a esa fecha: se baja de nuevo
        fx = motor.valor_en(motor.serie_cambio(moneda, DATOS, fresca=True), fecha_iso, margen=6)
    return fx


def movimiento_en_euros(datos):
    """Un movimiento con el importe en otra moneda (USD...) pasa a euros con el cambio
    de su fecha; en la nota queda lo que se pagó en la moneda original."""
    moneda = str(datos.get("moneda") or "EUR").strip().upper()
    if moneda in ("EUR", ""):
        return datos
    if not re.fullmatch(r"[A-Z]{3}", moneda):
        raise almacen.ErrorValidacion([f"La moneda «{moneda}» no es válida."])
    errores = []
    f = almacen.fecha(datos.get("fecha"), errores)
    importe = almacen.numero(datos.get("importe"), "el importe", errores, mayor_que=0)
    comision = almacen.numero(datos.get("comision"), "la comisión", errores, obligatorio=False, minimo=0)
    if errores:
        raise almacen.ErrorValidacion(errores)
    fx = cambio_del_dia(moneda, f)
    if not fx:
        raise almacen.ErrorValidacion([f"No encuentro el cambio de {moneda} a euros del {almacen.fmt_fecha(f)}. "
                                       "Comprueba tu conexión a internet o escribe el importe en euros."])
    original = f"{almacen.fmt_num(importe)} {moneda} (1 {moneda} = {almacen.fmt_num(fx)} €)"
    nota = almacen.texto(datos.get("nota"))
    return {**datos, "importe": round(importe * fx, 2), "comision": round(comision * fx, 2) if comision else None,
            "nota": (nota + " · " if nota else "") + original}


@app.get("/api/cambio")
def api_cambio():
    moneda = (request.args.get("moneda") or "").upper()
    fecha = request.args.get("fecha") or dt.date.today().isoformat()
    if not re.fullmatch(r"[A-Z]{3}", moneda) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        return jsonify(ok=False), 400
    with cerrojo:
        fx = 1.0 if moneda == "EUR" else cambio_del_dia(moneda, fecha)
    return jsonify(ok=bool(fx), cambio=fx)


@app.post("/api/<coleccion>")
def api_guardar(coleccion):
    if coleccion not in GUARDAR:
        return jsonify(ok=False, errores=["No sé guardar eso."]), 404
    datos = request.get_json(silent=True) or {}

    def fn(cfg):
        if coleccion == "movimientos":
            return GUARDAR[coleccion](cfg, movimiento_en_euros(datos))
        if coleccion != "productos":
            return GUARDAR[coleccion](cfg, datos)
        prod, cambio = almacen.guarda_producto(cfg, datos)
        # Antes de guardar un producto con precio online, se comprueba que lo hay.
        if cambio and prod["fuente"] != "manual" and not buscar.probar(prod["fuente"], prod["codigo"]):
            raise almacen.ErrorValidacion([
                f"No encuentro precio para «{prod['codigo']}» en {motor.FUENTES[prod['fuente']]}. "
                "Revisa el código con el buscador o elige «a mano» y anota tú su valor."])
        return prod
    return cambia(fn)


@app.delete("/api/<coleccion>/<ident>")
def api_borrar(coleccion, ident):
    if coleccion not in BORRAR:
        return jsonify(ok=False, errores=["No sé borrar eso."]), 404
    return cambia(lambda cfg: BORRAR[coleccion](cfg, ident))


@app.post("/api/objetivo")
def api_objetivo():
    datos = request.get_json(silent=True) or {}
    return cambia(lambda cfg: almacen.guarda_objetivo(cfg, datos))


@app.post("/api/colchon")
def api_colchon():
    datos = request.get_json(silent=True) or {}
    return cambia(lambda cfg: almacen.guarda_colchon(cfg, datos))


@app.post("/api/reparto")
def api_reparto():
    datos = request.get_json(silent=True) or {}
    return cambia(lambda cfg: almacen.guarda_reparto(cfg, datos))


@app.get("/api/unidades")
def api_unidades():
    """Unidades exactas que tenías de un producto un día (para «Vender todo»)."""
    pid, fecha = request.args.get("producto") or "", request.args.get("fecha") or dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        return jsonify(ok=False), 400
    cfg = cartera()
    if not almacen.producto(cfg, pid):
        return jsonify(ok=False, errores=["Ese producto ya no existe."]), 404
    return jsonify(ok=True, unidades=round(almacen.unidades_a(cfg, pid, fecha, excluir=request.args.get("excluir"),
                                                              cache=os.path.join(DATOS, "cache")), 8))


def _serie_de(cfg, datos):
    """Precio diario en euros del producto de una aportación periódica ({} si se valora a mano)."""
    p = almacen.producto(cfg, str(datos.get("producto") or ""))
    if not p or p.get("fuente") == "manual":
        return {}
    return motor.serie_producto(p, DATOS)


@app.post("/api/periodicas/ver")
def api_periodicas_ver():
    """Las aportaciones periódicas que se añadirían, con el precio de cada día. No guarda nada."""
    if bloqueado():
        return bloqueado()
    datos = request.get_json(silent=True) or {}
    with cerrojo:
        cfg = cartera()
        try:
            return jsonify(ok=True, plan=periodicas.plan(cfg, datos, _serie_de(cfg, datos)))
        except almacen.ErrorValidacion as e:
            return jsonify(ok=False, errores=e.errores), 400


@app.post("/api/periodicas/crear")
def api_periodicas_crear():
    datos = request.get_json(silent=True) or {}
    return cambia(lambda cfg: periodicas.crear(cfg, datos, _serie_de(cfg, datos)))


@app.post("/api/periodicas/olvidar")
def api_periodicas_olvidar():
    datos = request.get_json(silent=True) or {}
    return cambia(lambda cfg: periodicas.olvidar(cfg, datos))


@app.get("/api/fiscal")
def api_fiscal():
    """Resumen para la Renta de un año (orientativo). Se calcula aquí, con tus movimientos."""
    if modo() == "todos":
        return jsonify(ok=False, errores=["La declaración es de cada persona: elige un perfil arriba."]), 400
    ano = request.args.get("ano") or None
    if ano is not None and not re.fullmatch(r"\d{4}", ano):
        return jsonify(ok=False, errores=["Año no válido."]), 400
    with cerrojo:   # usa el motor, que no admite dos cálculos a la vez
        inf = fiscal.informe(cartera(), ano, cache=os.path.join(DATOS, "cache"))
    return jsonify(ok=True, csv=fiscal.csv_texto(inf), **inf)


@app.get("/api/precio")
def api_precio():
    """Precio en euros de un producto un día (para rellenar solo un traspaso)."""
    pid, fecha = request.args.get("producto") or "", request.args.get("fecha") or dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fecha):
        return jsonify(ok=False), 400
    p = almacen.producto(cartera(), pid)
    if not p or p.get("fuente") == "manual":
        return jsonify(ok=False)
    with cerrojo:
        serie = motor.serie_producto(p, DATOS)
    precio, cuando = motor.valor_en(serie, fecha, margen=7), motor.fecha_en(serie, fecha, margen=7)
    return jsonify(ok=bool(precio), precio=precio, fecha=cuando)


@app.post("/api/repartir-colores")
def api_repartir_colores():
    calc = lee_json(ruta_calculado()) or {}
    grandes = [p["id"] for p in sorted(calc.get("productos", []) + calc.get("otrosActivos", []),
                                       key=lambda p: -(p.get("valor") or 0))]
    return cambia(lambda cfg: almacen.reparte_colores(cfg, grandes))


@app.post("/api/empezar")
def api_empezar():
    """Sale de la demo: crea tu cartera, vacía o como copia del ejemplo para practicar."""
    if modo() != "demo":
        return jsonify(ok=False, errores=["Ya tienes tu propia cartera."]), 400
    with cerrojo:
        if (request.get_json(silent=True) or {}).get("desde") == "ejemplo":
            cfg = dict(lee_json(DEMO, {}), titular="Mi patrimonio (copia del ejemplo)")
        else:
            cfg = json.loads(json.dumps(almacen.CARTERA_VACIA))
        almacen.guarda(os.path.join(carpeta(), "cartera.json"), cfg)
        recalcula(descargar="faltan")
    return jsonify(ok=True)


# ---------------------------------------------------------------- importar

PLANES = {}   # vista previa pendiente de confirmar: {token: plan}


@app.post("/api/importar/previsualizar")
def api_importar_previsualizar():
    """Lee lo que se quiere importar y devuelve la vista previa, sin guardar nada."""
    if bloqueado():
        return bloqueado()
    origen = request.form.get("origen")
    archivos = [(f.filename, f.read()) for f in request.files.getlist("archivos") if f.filename]
    texto = (request.form.get("texto") or "").strip()
    with cerrojo:
        cfg = almacen.carga(os.path.join(carpeta(), "cartera.json"))
        if origen == "myinvestor":
            if not archivos:
                return jsonify(ok=False, errores=["Elige los archivos CSV que has descargado de MyInvestor."]), 400
            plan = importar.preparar_myinvestor(cfg, archivos, DATOS)
        else:
            if not archivos and not texto:
                return jsonify(ok=False, errores=["Elige un archivo o pega el texto que te ha dado la IA."]), 400
            filas, error = [], None
            for nombre, contenido in archivos or [("pegado.csv", texto)]:
                leidas, error = importar.leer_tabla(nombre, contenido)
                if error:
                    return jsonify(ok=False, errores=[f"{nombre}: {error}" if archivos else error]), 400
                filas += leidas
            plan = importar.preparar_tabla(cfg, filas, DATOS)
        informe = importar.vista_previa(cfg, plan, None, DATOS)
    token = secrets.token_hex(8)
    PLANES.clear()   # solo una importación pendiente a la vez
    PLANES[token] = plan
    return jsonify(ok=True, token=token, informe=informe)


def _respuestas(datos):
    """Respuestas a «¿de qué fondo venía este traspaso?»: {ref: {"origen", "fecha"}}."""
    r = datos.get("respuestas")
    if not isinstance(r, dict):
        return {}
    return {str(k)[:80]: {"origen": str(v.get("origen") or "")[:80], "fecha": str(v.get("fecha") or "")[:10],
                          **({"fechas": [str(f or "")[:10] for f in v["fechas"][:60]]}
                             if isinstance(v.get("fechas"), list) else {})}
            for k, v in r.items() if isinstance(v, dict)}


@app.post("/api/importar/revisar")
def api_importar_revisar():
    """La misma vista previa, con las respuestas sobre los traspasos de MyInvestor."""
    if bloqueado():
        return bloqueado()
    datos = request.get_json(silent=True) or {}
    plan = PLANES.get(datos.get("token"))
    if plan is None:
        return jsonify(ok=False, errores=["Esa vista previa ya no vale: vuelve a revisar el archivo."]), 400
    with cerrojo:
        cfg = almacen.carga(os.path.join(carpeta(), "cartera.json"))
        informe = importar.vista_previa(cfg, plan, _respuestas(datos), DATOS)
    return jsonify(ok=True, token=datos.get("token"), informe=informe)


@app.post("/api/importar/confirmar")
def api_importar_confirmar():
    datos_peticion = request.get_json(silent=True) or {}
    plan = PLANES.pop(datos_peticion.get("token"), None)
    if plan is None:
        return jsonify(ok=False, errores=["Esa vista previa ya no vale: vuelve a revisar el archivo."]), 400
    informe = {}
    respuestas = _respuestas(datos_peticion)

    def fn(cfg):
        informe.update(importar.aplicar(cfg, plan, respuestas, DATOS))
        return None
    respuesta = cambia(fn)
    if isinstance(respuesta, tuple):
        return respuesta
    datos = respuesta.get_json()
    datos["informe"] = {k: informe[k] for k in ("añadidos", "repetidos", "saldos", "sustituidos")}
    datos["informe"]["traspasos"] = sum(1 for t in informe.get("traspasosMyInvestor", []) if t["hecho"])
    return jsonify(datos)


@app.get("/api/plantilla.xlsx")
def api_plantilla_xlsx():
    return Response(plantilla.excel(), headers={"Content-Disposition": 'attachment; filename="plantilla_patrimonio.xlsx"'},
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/plantilla.csv")
def api_plantilla_csv():
    return Response(plantilla.csv_vacio(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="plantilla_patrimonio.csv"'})


@app.get("/api/exportar-excel")
def api_exportar_excel():
    """Tus movimientos, saldos y productos en un Excel (se puede volver a importar)."""
    nombre = f"rumbo_mis_datos_{dt.date.today().isoformat()}.xlsx"
    return Response(plantilla.mis_datos(cartera()),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.get("/api/prompt")
def api_prompt():
    with open(os.path.join(RAIZ, "app", "prompt_ia.txt"), encoding="utf-8") as f:
        return jsonify(texto=f.read())


# ---------------------------------------------------------------- copias de seguridad

def ruta_copias():
    return os.path.join(carpeta(), "copias")


ID_OK = re.compile(r"[A-Za-z0-9_.\-]{1,80}")
FECHA_OK = re.compile(r"\d{4}-\d{2}-\d{2}")


def valida_copia(cfg):
    """Comprueba que un JSON es de verdad una cartera de esta app, pieza a pieza: una copia
    manipulada no puede colar productos, fechas o importes raros."""
    malo = almacen.ErrorValidacion(["Ese archivo no es una copia de seguridad de esta app (o está dañado)."])
    if not isinstance(cfg, dict) or not all(isinstance(cfg.get(k), list)
                                            for k in ("productos", "movimientos", "valoraciones")):
        raise malo
    numero = lambda x: x is None or (isinstance(x, (int, float)) and not isinstance(x, bool))
    ids = set()
    for p in cfg["productos"]:
        if not isinstance(p, dict) or not ID_OK.fullmatch(str(p.get("id") or "")) \
                or not isinstance(p.get("nombre", ""), str):
            raise malo
        ids.add(p["id"])
    for m in cfg["movimientos"]:
        if not isinstance(m, dict) or not FECHA_OK.fullmatch(str(m.get("fecha") or "")) \
                or m.get("producto") not in ids or (m.get("destino") is not None and m["destino"] not in ids) \
                or not all(numero(m.get(k)) for k in ("importe", "unidades", "unidadesDestino", "comision")):
            raise malo
    for v in cfg["valoraciones"]:
        if not isinstance(v, dict) or not FECHA_OK.fullmatch(str(v.get("fecha") or "")) \
                or v.get("producto") not in ids or not numero(v.get("valor")) or not numero(v.get("aportado")):
            raise malo
    return cfg


def restaura(cfg):
    """Pone cfg como cartera. Lo que hubiera antes queda en las copias automáticas."""
    with cerrojo:
        almacen.guarda(os.path.join(carpeta(), "cartera.json"), cfg)
        for viejo in ("calculado_propio.json", "historico.json"):
            if os.path.exists(os.path.join(carpeta(), viejo)):
                os.remove(os.path.join(carpeta(), viejo))
        recalcula(descargar="faltan")


@app.get("/api/copias")
def api_copias():
    lista = []
    if modo() != "todos" and os.path.isdir(ruta_copias()):
        for n in os.listdir(ruta_copias()):
            if not n.endswith(".json"):
                continue
            ruta = os.path.join(ruta_copias(), n)
            cfg = lee_json(ruta, {}) or {}
            lista.append({"archivo": n,
                          "fecha": dt.datetime.fromtimestamp(os.path.getmtime(ruta)).isoformat(timespec="minutes"),
                          "motivo": "Antes de empezar de nuevo" if n.startswith("antes_de_reiniciar")
                          else "Antes de recuperar una copia" if n.startswith("antes_de_recuperar")
                          else "Automática",
                          "productos": len(cfg.get("productos", [])),
                          "movimientos": len(cfg.get("movimientos", []))})
    lista.sort(key=lambda c: c["fecha"], reverse=True)
    return jsonify(copias=lista)


@app.get("/api/copia/descargar")
def api_copia_descargar():
    if modo() != "propio":
        return jsonify(ok=False, errores=["Todavía no tienes una cartera propia que guardar."]), 400
    with open(os.path.join(carpeta(), "cartera.json"), "rb") as f:
        contenido = f.read()
    nombre = f"copia_patrimonio_{dt.date.today().isoformat()}.json"
    return Response(contenido, mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.post("/api/copia/subir")
def api_copia_subir():
    """Importa una copia descargada antes (por ejemplo, al cambiar de ordenador)."""
    if modo() == "todos":
        return bloqueado()
    f = request.files.get("archivo")
    if not f:
        return jsonify(ok=False, errores=["Elige el archivo de la copia (termina en .json)."]), 400
    try:
        cfg = valida_copia(json.loads(importar.decodifica(f.read())))
    except ValueError:
        return jsonify(ok=False, errores=["Ese archivo no es una copia de seguridad de esta app."]), 400
    except almacen.ErrorValidacion as e:
        return jsonify(ok=False, errores=e.errores), 400
    restaura(cfg)
    return jsonify(ok=True)


@app.post("/api/copia/recuperar")
def api_copia_recuperar():
    if modo() == "todos":
        return bloqueado()
    nombre = os.path.basename((request.get_json(silent=True) or {}).get("archivo") or "")
    ruta = os.path.join(ruta_copias(), nombre)
    if not nombre.endswith(".json") or not os.path.exists(ruta):
        return jsonify(ok=False, errores=["Esa copia ya no existe."]), 400
    try:
        cfg = valida_copia(lee_json(ruta))
    except almacen.ErrorValidacion as e:
        return jsonify(ok=False, errores=e.errores), 400
    if modo() == "propio":
        # Además de la copia automática, una con nombre propio que no se borra sola.
        sello = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(carpeta(), "cartera.json"), "rb") as a, \
                open(os.path.join(ruta_copias(), f"antes_de_recuperar_{sello}.json"), "wb") as b:
            b.write(a.read())
    restaura(cfg)
    return jsonify(ok=True)


# ---------------------------------------------------------------- web estática y versión

@app.get("/api/exportar-web")
def api_exportar_web():
    datos = lee_json(ruta_calculado())
    if not datos:
        return jsonify(ok=False, errores=["Todavía no hay nada que exportar."]), 400
    ocultar = request.args.get("ocultar") == "1"
    html = exportar.pagina(WEB, datos, ocultar=ocultar, titulo=datos.get("titular") or "Mi patrimonio")
    nombre = "patrimonio_sin_importes.html" if ocultar else "patrimonio.html"
    return Response(html.encode("utf-8"), mimetype="text/html",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


REPO = "https://github.com/danidm98/rumbo"
_VERSION = {}


def version_actual():
    with open(os.path.join(RAIZ, "app", "VERSION"), encoding="utf-8") as f:
        return f.read().strip()


@app.get("/api/version")
def api_version():
    """Compara esta versión con la publicada en GitHub (se consulta como mucho una vez al día)."""
    actual = version_actual()
    if not _VERSION or dt.datetime.now() - _VERSION["cuando"] > dt.timedelta(hours=24):
        try:
            url = REPO.replace("github.com", "raw.githubusercontent.com") + "/main/app/VERSION"
            with red.abrir(url, motor.UA, timeout=5) as r:
                _VERSION.update(ultima=r.read().decode().strip(), cuando=dt.datetime.now())
        except Exception:
            _VERSION.update(ultima=None, cuando=dt.datetime.now())
    ultima = _VERSION.get("ultima")
    return jsonify(actual=actual, ultima=ultima, repo=REPO, docker=en_docker(),
                   hayNueva=bool(ultima) and como_tupla(ultima) > como_tupla(actual))


def como_tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


# Novedades de cada versión (app/novedades.json). La de GitHub se lee una vez al día:
# así el enlace al vídeo de una versión se puede añadir después de publicarla.
_NOVEDADES = {}
VIDEO_OK = re.compile(r"^https://(www\.)?(youtube\.com|youtu\.be)/[A-Za-z0-9_\-?=&/.%]+$")


def valida_novedades(n):
    """Solo lo que la app sabe pintar: textos sin HTML y un enlace de YouTube."""
    if not isinstance(n, dict) or not re.fullmatch(r"\d+\.\d+\.\d+", str(n.get("version") or "")):
        return None
    puntos = [motor.sin_html(x)[:300] for x in (n.get("novedades") or []) if isinstance(x, str)][:15]
    video = n.get("video") if isinstance(n.get("video"), str) and VIDEO_OK.match(n["video"]) else None
    return {"version": n["version"], "titulo": motor.sin_html(str(n.get("titulo") or ""))[:120],
            "novedades": puntos, "video": video}


@app.get("/api/novedades")
def api_novedades():
    local = valida_novedades(lee_json(os.path.join(RAIZ, "app", "novedades.json")))
    if not _NOVEDADES or dt.datetime.now() - _NOVEDADES["cuando"] > dt.timedelta(hours=24):
        remota = None
        try:
            url = REPO.replace("github.com", "raw.githubusercontent.com") + "/main/app/novedades.json"
            with red.abrir(url, motor.UA, timeout=5) as r:
                remota = valida_novedades(json.loads(r.read().decode("utf-8")))
        except Exception:
            pass
        _NOVEDADES.update(remota=remota, cuando=dt.datetime.now())
    remota, actual = _NOVEDADES.get("remota"), version_actual()
    # Lo de tu versión: el archivo que trae la app, con el vídeo de GitHub si ya lo hay.
    tuya = local if local and local["version"] == actual else None
    if tuya and remota and remota["version"] == actual and remota.get("video"):
        tuya = dict(tuya, video=remota["video"])
    nueva = remota if remota and como_tupla(remota["version"]) > como_tupla(actual) else None
    return jsonify(actual=actual, tuya=tuya, nueva=nueva, repo=REPO, docker=en_docker())


@app.post("/api/reiniciar")
def api_reiniciar():
    """Empieza de cero o vuelve a la demo. Lo que había se guarda antes en mis_datos/copias."""
    if modo() != "propio":
        return jsonify(ok=False, errores=["Ahora mismo no tienes ninguna cartera propia."]), 400
    a = (request.get_json(silent=True) or {}).get("a")
    if perfiles()["activo"] != PRINCIPAL:
        a = "vacia"   # la cartera de ejemplo es solo del perfil principal
    with cerrojo:
        ruta = os.path.join(carpeta(), "cartera.json")
        copias = os.path.join(carpeta(), "copias")
        os.makedirs(copias, exist_ok=True)
        sello = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.replace(ruta, os.path.join(copias, f"antes_de_reiniciar_{sello}.json"))
        for viejo in ("calculado_propio.json", "historico.json"):
            if os.path.exists(os.path.join(carpeta(), viejo)):
                os.remove(os.path.join(carpeta(), viejo))
        if a == "vacia":
            almacen.guarda(ruta, json.loads(json.dumps(almacen.CARTERA_VACIA)))
        recalcula(descargar="faltan")
    return jsonify(ok=True)


def info_perfiles():
    d = perfiles()
    hay = {x["id"] for x in con_cartera(d["lista"])}
    return {"activo": d["activo"], "todos": len(hay) >= 2,
            "lista": [{**x, "conDatos": x["id"] in hay} for x in d["lista"]]}


@app.get("/api/perfiles")
def api_perfiles():
    return jsonify(ok=True, **info_perfiles())


@app.post("/api/perfiles")
def api_perfil_nuevo():
    nombre = almacen.texto((request.get_json(silent=True) or {}).get("nombre"), 40)
    if not nombre:
        return jsonify(ok=False, errores=["Ponle un nombre al perfil (por ejemplo, el de tu pareja)."]), 400
    with cerrojo:
        d = perfiles()
        if any(x["nombre"].lower() == nombre.lower() for x in d["lista"]):
            return jsonify(ok=False, errores=[f"Ya tienes un perfil que se llama «{nombre}»."]), 400
        pid = almacen.slug(nombre, {x["id"] for x in d["lista"]} | {PRINCIPAL, TODOS})
        cfg = json.loads(json.dumps(almacen.CARTERA_VACIA))
        cfg["titular"] = nombre
        os.makedirs(carpeta(pid), exist_ok=True)
        almacen.guarda(os.path.join(carpeta(pid), "cartera.json"), cfg)
        d["lista"].append({"id": pid, "nombre": nombre})
        d["activo"] = pid
        guarda_perfiles(d)
    return jsonify(ok=True, id=pid)


@app.post("/api/perfiles/activar")
def api_perfil_activar():
    pid = (request.get_json(silent=True) or {}).get("id")
    with cerrojo:
        d = perfiles()
        if pid != TODOS and not any(x["id"] == pid for x in d["lista"]):
            return jsonify(ok=False, errores=["Ese perfil ya no existe."]), 404
        if pid == TODOS and len(con_cartera(d["lista"])) < 2:
            return jsonify(ok=False, errores=["«Todos juntos» necesita al menos dos perfiles con datos."]), 400
        d["activo"] = pid
        guarda_perfiles(d)
        recalcula(descargar="faltan")
    return jsonify(ok=True)


@app.post("/api/perfiles/renombrar")
def api_perfil_renombrar():
    datos = request.get_json(silent=True) or {}
    nombre = almacen.texto(datos.get("nombre"), 40)
    with cerrojo:
        d = perfiles()
        per = next((x for x in d["lista"] if x["id"] == datos.get("id")), None)
        if not per:
            return jsonify(ok=False, errores=["Ese perfil ya no existe."]), 404
        if not nombre:
            return jsonify(ok=False, errores=["Ponle un nombre al perfil."]), 400
        per["nombre"] = nombre
        guarda_perfiles(d)
    return jsonify(ok=True)


@app.post("/api/perfiles/borrar")
def api_perfil_borrar():
    """Borra un perfil. Su carpeta no se pierde: se mueve a mis_datos/copias."""
    pid = (request.get_json(silent=True) or {}).get("id")
    with cerrojo:
        d = perfiles()
        if pid == PRINCIPAL or not any(x["id"] == pid for x in d["lista"]):
            return jsonify(ok=False, errores=["Ese perfil no se puede borrar."]), 400
        origen = carpeta(pid)
        if os.path.isdir(origen):
            destino = os.path.join(DATOS, "copias", f"perfil_borrado_{pid}_{dt.datetime.now():%Y%m%d_%H%M%S}")
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            os.replace(origen, destino)
        d["lista"] = [x for x in d["lista"] if x["id"] != pid]
        if d["activo"] in (pid, TODOS):
            d["activo"] = PRINCIPAL
        guarda_perfiles(d)
        recalcula(descargar="faltan")
    return jsonify(ok=True)


# Los últimos errores, para el informe de soporte (sin datos de tu cartera).
ERRORES = []


class _GuardaErrores(logging.Handler):
    def emit(self, registro):
        texto = self.format(registro)
        ERRORES.append(f"{dt.datetime.now():%Y-%m-%d %H:%M} {texto}"[-1500:])
        del ERRORES[:-5]


_manejador = _GuardaErrores(level=logging.ERROR)
_manejador.setFormatter(logging.Formatter("%(message)s\n%(exc_text)s"))
logging.getLogger().addHandler(_manejador)
app.logger.addHandler(_manejador)


@app.get("/api/diagnostico")
def api_diagnostico():
    """Lo que hace falta para ayudarte con un fallo, sin importes ni nombres de tu cartera."""
    import platform
    calc = lee_json(ruta_calculado()) or {}
    cfg = cartera()
    tipos = {}
    for p in cfg.get("productos", []):
        clave = f"{p.get('tipo')}/{p.get('fuente')}"
        tipos[clave] = tipos.get(clave, 0) + 1
    texto = "\n".join([
        f"Rumbo {version_actual()} · {platform.system()} {platform.release()} ({platform.machine()}) · Python {platform.python_version()}",
        f"Modo: {modo()} · perfiles: {len(perfiles()['lista'])} · productos: {len(cfg.get('productos', []))} {tipos}"
        f" · movimientos: {len(cfg.get('movimientos', []))} · valores anotados: {len(cfg.get('valoraciones', []))}",
        f"Precios actualizados: {estado().get('preciosActualizados') or 'nunca'} · datos a {calc.get('fechaExtracto') or '—'}",
        f"Avisos en el panel: {len(calc.get('avisos') or [])}",
        "Últimos errores:" + ("\n" + "\n".join(ERRORES) if ERRORES else " ninguno"),
    ])
    return jsonify(ok=True, texto=texto)


@app.get("/api/ping")
def api_ping():
    return jsonify(app="patrimonio")


@app.get("/<path:archivo>")
def estaticos(archivo):
    return send_from_directory(WEB, archivo)


# ---------------------------------------------------------------- arranque

def ya_abierta(puerto):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/ping", timeout=1) as r:
            return json.load(r).get("app") == "patrimonio"
    except Exception:
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # Si la app ya está abierta (otra ventana), basta con enseñarla.
    if ya_abierta(PUERTO):
        print("La app ya estaba abierta: te la enseño en el navegador.")
        if not os.environ.get("PATRIMONIO_NO_ABRIR"):
            webbrowser.open(url_con_llave(PUERTO))
        return

    print("\n  RUMBO  ·  tu patrimonio neto")
    print("  " + "-" * 40)
    if modo() == "demo":
        print("  Modo demostración: estás viendo una cartera de ejemplo.")
    # Siempre se recalcula al arrancar (sin internet es un momento): así, tras
    # actualizar la app a una versión nueva, el panel nunca usa cálculos viejos.
    viejos = precios_viejos()
    if viejos:
        print("  Actualizando precios (tarda unos segundos)...")
    try:
        recalcula(descargar=viejos or "faltan")
        completa_exposiciones()
    except Exception as e:
        print(f"\n  [!] No he podido actualizar los precios: {e}")
        print("      Abro la app con los últimos datos guardados.")

    srv = None
    host = escucha_en()
    for puerto in (range(PUERTO, PUERTO + 10) if not en_red() else [PUERTO]):
        try:
            srv = make_server(host, puerto, app, threaded=True)
            break
        except OSError:
            continue
    if srv is None:
        print("  [!] No encuentro ningún puerto libre para abrir la app.")
        return
    url = f"http://127.0.0.1:{srv.server_port}/"
    if en_red():
        # Docker en un NAS o una Raspberry: se entra desde el navegador de otro equipo de casa.
        print(f"\n  Rumbo está abierta en tu red de casa (puerto {srv.server_port}).")
        print("  Ábrela desde el navegador de cualquier ordenador de casa con esta dirección,")
        print("  cambiando IP-DEL-EQUIPO por la del NAS o la Raspberry (por ejemplo, 192.168.1.50):")
        print(f"\n      http://IP-DEL-EQUIPO:{srv.server_port}/?acceso={token_acceso()}\n")
        print("  La llave también está en el archivo .acceso de tus datos. Solo hace falta la primera")
        print("  vez en cada navegador. NO abras este puerto a internet: para entrar desde fuera de casa,")
        print("  usa una VPN como Tailscale.\n")
    else:
        if not os.environ.get("PATRIMONIO_NO_ABRIR"):   # para pruebas: no abre el navegador
            threading.Timer(0.8, webbrowser.open, [url_con_llave(srv.server_port)]).start()
        print(f"\n  App abierta en {url}")
        print("  Si cierras la pestaña, vuelve a abrirla con el lanzador (Iniciar): lleva tu llave de acceso.")
        print("  Deja esta ventana abierta mientras la uses. Para salir, ciérrala.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
