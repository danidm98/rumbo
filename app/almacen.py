# -*- coding: utf-8 -*-
"""
almacen.py  ·  Guarda y valida la cartera
=========================================
Todo lo que se escribe en mis_datos/cartera.json pasa por aquí: se comprueba
que tiene sentido y, antes de cada cambio, se guarda una copia automática.
"""

import datetime as dt
import json
import os
import re
import unicodedata

from . import motor
from .motor import TIPOS, FUENTES, num_es

COPIAS_MAX = 20
TIPOS_MOV = {"compra": "Compra", "venta": "Venta", "dividendo": "Dividendo, cupón o interés",
             "comision": "Comisión", "traspaso": "Traspaso", "ajuste": "Ajuste de participaciones"}
SOLO_SALDO = ("efectivo", "deuda")   # tipos que se siguen solo con saldos anotados

CARTERA_VACIA = {
    "version": 1, "titular": "Mi patrimonio",
    "productos": [], "movimientos": [], "valoraciones": [],
    "comparador": [{"id": "real", "nombre": "Mi cartera real", "real": True}],
    "hitos": [10000, 25000, 50000, 100000, 250000, 500000, 1000000],
    "objetivo": {"activo": False, "importe": None, "etiqueta": "Mi objetivo"},
}


class ErrorValidacion(Exception):
    def __init__(self, errores):
        super().__init__("; ".join(errores))
        self.errores = errores


# ---------------------------------------------------------------- disco

def carga(ruta):
    with open(ruta, "r", encoding="utf-8") as f:
        return json.load(f)


def guarda(ruta, cfg):
    """Copia de seguridad automática de lo que había y escritura atómica de lo nuevo."""
    carpeta = os.path.dirname(ruta)
    copias = os.path.join(carpeta, "copias")
    if os.path.exists(ruta):
        os.makedirs(copias, exist_ok=True)
        sello = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destino, n = os.path.join(copias, f"auto_{sello}.json"), 1
        while os.path.exists(destino):   # dos guardados en el mismo instante (el reloj de Windows va a saltos)
            destino, n = os.path.join(copias, f"auto_{sello}_{n}.json"), n + 1
        with open(ruta, "rb") as f, open(destino, "wb") as g:
            g.write(f.read())
        viejas = sorted(n for n in os.listdir(copias) if n.startswith("auto_"))
        for n in viejas[:-COPIAS_MAX]:
            os.remove(os.path.join(copias, n))
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ruta)


# ---------------------------------------------------------------- ayudas

def fmt_fecha(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}"


def fmt_num(x):
    """12345.6 -> '12.345,6' (hasta 4 decimales, sin ceros de sobra)."""
    t = f"{x:,.4f}".rstrip("0").rstrip(".")
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_unidades(x):
    """Unidades con todos sus decimales (hasta 8): 12,34567891."""
    t = f"{x:,.8f}".rstrip("0").rstrip(".")
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def numero(valor, etiqueta, errores, obligatorio=True, minimo=None, mayor_que=None):
    """Acepta 1234.5, "1.234,56" o "1234,5". Devuelve float o None."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if obligatorio:
            errores.append(f"Falta {etiqueta}.")
        return None
    if isinstance(valor, (int, float)):
        x = float(valor)
    else:
        t = valor.strip().replace("€", "").replace("%", "").replace(" ", "")
        if not re.fullmatch(r"-?[\d.,]+", t):
            errores.append(f"{etiqueta.capitalize()} no es un número: «{valor}».")
            return None
        x = num_es(t)
    if minimo is not None and x < minimo:
        errores.append(f"{etiqueta.capitalize()} no puede ser menor que {minimo:g}.")
    if mayor_que is not None and x <= mayor_que:
        errores.append(f"{etiqueta.capitalize()} tiene que ser mayor que {mayor_que:g}.")
    return x


def fecha(valor, errores):
    try:
        f = dt.date.fromisoformat(str(valor or "")[:10])
    except ValueError:
        errores.append("Falta la fecha o no es válida.")
        return None
    if f > dt.date.today():
        errores.append(f"La fecha {fmt_fecha(f.isoformat())} es futura.")
    elif f.year < 1950:
        errores.append(f"La fecha {fmt_fecha(f.isoformat())} parece un error.")
    return f.isoformat()


def slug(texto, existentes):
    base = unicodedata.normalize("NFKD", texto or "producto").encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")[:30] or "producto"
    cand, i = base, 2
    while cand in existentes:
        cand, i = f"{base}_{i}", i + 1
    return cand


def siguiente_id(lista, prefijo):
    nums = [int(x["id"][1:]) for x in lista if re.fullmatch(prefijo + r"\d+", str(x.get("id", "")))]
    return f"{prefijo}{max(nums, default=0) + 1}"


def producto(cfg, pid):
    return next((p for p in cfg.get("productos", []) if p["id"] == pid), None)


def texto(v, maximo=200):
    """Texto libre del usuario, sin < ni > (el panel lo pinta como HTML)."""
    return re.sub(r"[<>]", "", str(v)).strip()[:maximo] if v is not None else ""


# ---------------------------------------------------------------- objetivo

def guarda_objetivo(cfg, datos):
    """Tu propio objetivo de patrimonio. Desactivado, el panel usa el siguiente hito."""
    activo = bool(datos.get("activo"))
    errores = []
    importe = numero(datos.get("importe"), "el importe del objetivo", errores, obligatorio=activo, mayor_que=0)
    if errores:
        raise ErrorValidacion(errores)
    cfg["objetivo"] = {"activo": activo, "importe": round(importe, 2) if importe else None,
                       "etiqueta": texto(datos.get("etiqueta"), 60) or "Mi objetivo", "elegido": True}
    return cfg["objetivo"]


# ---------------------------------------------------------------- fondo de emergencia

def guarda_colchon(cfg, datos):
    """Tu fondo de emergencia: X meses de gastos o un importe fijo."""
    if datos.get("borrar"):
        cfg.pop("colchon", None)
        return None
    errores = []
    modo = "importe" if datos.get("modo") == "importe" else "meses"
    if modo == "meses":
        meses = numero(datos.get("meses"), "cuántos meses de gastos", errores, mayor_que=0)
        gasto = numero(datos.get("gastoMensual"), "lo que gastas al mes", errores, mayor_que=0)
        if meses and meses > 60:
            errores.append("Más de 60 meses no parece un fondo de emergencia: revisa el número.")
        if errores:
            raise ErrorValidacion(errores)
        cfg["colchon"] = {"modo": "meses", "meses": round(meses, 1), "gastoMensual": round(gasto, 2)}
    else:
        importe = numero(datos.get("importe"), "el importe del fondo de emergencia", errores, mayor_que=0)
        if errores:
            raise ErrorValidacion(errores)
        cfg["colchon"] = {"modo": "importe", "importe": round(importe, 2)}
    return cfg["colchon"]


# ---------------------------------------------------------------- reparto objetivo (rebalanceo)

def guarda_reparto(cfg, datos):
    """Tu reparto objetivo: qué % quieres en cada clase de activo (o en cada producto)."""
    errores = []
    por = datos.get("por") if datos.get("por") in ("clase", "producto") else "clase"
    pesos = {}
    for k, v in (datos.get("pesos") or {}).items():
        x = numero(v, f"el % de «{texto(k, 40)}»", errores, obligatorio=False, minimo=0)
        if x:
            pesos[texto(k, 80)] = round(x, 2)
    if not pesos and not datos.get("borrar"):
        errores.append("Pon el % que quieres en al menos un grupo.")
    elif pesos and abs(sum(pesos.values()) - 100) > 0.5:
        errores.append(f"Los porcentajes suman {fmt_num(sum(pesos.values()))} %: tienen que sumar 100 %.")
    vigilar = numero(datos.get("vigilar"), "el margen para vigilar", errores, obligatorio=False, minimo=0) or 3
    rebalancear = numero(datos.get("rebalancear"), "el margen para rebalancear", errores, obligatorio=False, minimo=0) or 5
    if rebalancear < vigilar:
        errores.append("El margen para rebalancear tiene que ser mayor que el de vigilar.")
    if errores:
        raise ErrorValidacion(errores)
    if datos.get("borrar"):
        cfg.pop("objetivoReparto", None)
        return None
    # Productos que el usuario mete o saca del reparto a mano (id -> cuenta o no), por encima de la regla general.
    cuenta = datos.get("cuenta") if isinstance(datos.get("cuenta"), dict) else {}
    cuenta = {texto(k, 80): bool(v) for k, v in list(cuenta.items())[:300]}
    cfg["objetivoReparto"] = {"por": por, "pesos": pesos, "vigilar": vigilar, "rebalancear": rebalancear,
                              "conEfectivo": bool(datos.get("conEfectivo")), **({"cuenta": cuenta} if cuenta else {})}
    return cfg["objetivoReparto"]


# ---------------------------------------------------------------- productos

CAMPOS_TEXTO = ("nombre", "corto", "identificador", "codigo", "entidad", "clase", "gestora",
                "tipoDetalle", "respaldo", "respaldoMoneda", "vivo", "papel", "expoComo")


def guarda_producto(cfg, datos):
    """Crea o actualiza un producto. Devuelve (producto, cambió_la_fuente)."""
    errores = []
    nuevo = {k: texto(datos.get(k), 400 if k == "papel" else 200) for k in CAMPOS_TEXTO}
    if not nuevo["nombre"]:
        errores.append("Ponle un nombre al producto.")
    tipo = datos.get("tipo")
    if tipo not in TIPOS:
        errores.append("Elige de qué tipo es el producto.")
    fuente = "manual" if tipo in SOLO_SALDO else datos.get("fuente") or "manual"
    if fuente not in FUENTES:
        errores.append("Elige de dónde sale el precio.")
    if fuente != "manual" and not nuevo["codigo"]:
        errores.append(f"Falta el código del producto en {FUENTES.get(fuente, fuente)}. "
                       "Búscalo con el buscador o elige «a mano».")
    moneda = texto(datos.get("moneda")) or "EUR"
    moneda = moneda if moneda in ("GBp", "GBX") else moneda.upper()
    if not re.fullmatch(r"[A-Z]{3}|GBp|GBX", moneda):
        errores.append(f"La moneda «{moneda}» no es válida: usa un código de tres letras, como EUR o USD.")
    ter = numero(datos.get("ter"), "la comisión anual", errores, obligatorio=False, minimo=0)
    riesgo = numero(datos.get("riesgo"), "el nivel de riesgo", errores, obligatorio=False, minimo=1)
    if riesgo is not None and riesgo > 7:
        errores.append("El nivel de riesgo va de 1 a 7.")
    if errores:
        raise ErrorValidacion(errores)

    nuevo.update(tipo=tipo, fuente=fuente, moneda=moneda,
                 largoPlazo=bool(datos.get("largoPlazo", True)),
                 liquidez=bool(datos["liquidez"]) if "liquidez" in datos else None,
                 slot=int(datos["slot"]) if str(datos.get("slot") or "").isdigit() else None,
                 ter=ter / 100 if ter is not None else None,
                 riesgo=int(riesgo) if riesgo is not None else None)
    if fuente == "manual":
        nuevo.update(codigo="", respaldo="", respaldoMoneda="")
    nuevo["corto"] = nuevo["corto"] or nuevo["nombre"][:24]
    nuevo["expoComo"] = nuevo["expoComo"].replace(" ", "").upper()
    if nuevo["expoComo"] and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", nuevo["expoComo"]):
        raise ErrorValidacion([f"«{nuevo['expoComo']}» no parece un ISIN (12 caracteres, como IE00BYX5NX33)."])

    existente = producto(cfg, datos.get("id")) if datos.get("id") else None
    if existente:
        cambio = (existente.get("fuente"), existente.get("codigo")) != (fuente, nuevo["codigo"])
        destino = existente
        if nuevo["slot"] is None:
            nuevo.pop("slot")   # si no se dice color, se queda el que tenía
    else:
        cambio = fuente != "manual"
        destino = {"id": slug(nuevo["corto"], {p["id"] for p in cfg["productos"]})}
        if nuevo["slot"] is None:
            # Sin color elegido (por ejemplo, al importar): el que menos se use.
            usos = [sum(1 for p in cfg["productos"] if p.get("slot") == s) for s in range(1, 13)]
            nuevo["slot"] = usos.index(min(usos)) + 1
        cfg["productos"].append(destino)
    # Se conservan los campos que el formulario no toca (la exposición del índice, etc.).
    for k, v in nuevo.items():
        if v in ("", None):
            destino.pop(k, None)
        else:
            destino[k] = v
    return destino, cambio


def reparte_colores(cfg, orden=None):
    """Un color distinto para cada producto (los primeros de 'orden', los más grandes,
    se quedan los colores más fáciles de distinguir). Con más de 12, se repiten."""
    ids = [p["id"] for p in cfg["productos"]]
    orden = [i for i in (orden or []) if i in ids] + [i for i in ids if i not in (orden or [])]
    for n, pid in enumerate(orden):
        producto(cfg, pid)["slot"] = n % 12 + 1


def borra_producto(cfg, pid):
    """Borra el producto con sus movimientos y valores anotados."""
    if not producto(cfg, pid):
        raise ErrorValidacion(["Ese producto ya no existe."])
    cfg["productos"] = [p for p in cfg["productos"] if p["id"] != pid]
    cfg["movimientos"] = [m for m in cfg.get("movimientos", []) if pid not in (m.get("producto"), m.get("destino"))]
    cfg["valoraciones"] = [v for v in cfg.get("valoraciones", []) if v.get("producto") != pid]
    for c in cfg.get("comparador", []):
        if c.get("pesos"):
            c["pesos"].pop(pid, None)
    cfg["comparador"] = [c for c in cfg.get("comparador", []) if c.get("real") or c.get("pesos")]


# ---------------------------------------------------------------- movimientos

def unidades_a(cfg, pid, fecha_iso, excluir=None, cache=None):
    """Unidades que tenías de un producto al final de ese día (con los traspasos que entran y salen).
    Si ha habido splits, son las de ese día: las de antes del split se multiplican por él
    (ver motor.ajusta_splits; los splits se deducen con todos los movimientos, también los de
    después). 'cache' es la carpeta de precios (por defecto, la del motor)."""
    total = 0.0
    orden = {"compra": 0, "comision": 1, "dividendo": 2, "venta": 3, "traspaso": 4, "ajuste": 6}
    movs = [m for m in cfg.get("movimientos", []) if m.get("id") != excluir]
    movs, _ = motor.ajusta_splits(movs, cfg.get("productos", []), cache, hasta=fecha_iso)
    movs = [m for m in movs if m["fecha"] <= fecha_iso]
    for m in sorted(movs, key=lambda m: (m["fecha"], orden.get(m.get("tipo"), 9))):
        t = m.get("tipo")
        if m.get("producto") == pid:
            u = float(m.get("unidades") or 0)
            if t == "ajuste":
                total = u   # el ajuste dice cuántas tienes, no cuántas cambian
            else:
                total += u if t == "compra" else -u if t in ("venta", "traspaso") else 0
        if t == "traspaso" and m.get("destino") == pid:
            total += float(m.get("unidadesDestino") or 0)
    return total


def guarda_movimiento(cfg, datos):
    if datos.get("tipo") == "traspaso":
        return guarda_traspaso(cfg, datos)
    if datos.get("tipo") == "ajuste":
        return guarda_ajuste(cfg, datos)
    errores = []
    p = producto(cfg, datos.get("producto"))
    if not p:
        errores.append("Elige el producto.")
    tipo = datos.get("tipo")
    if p and p.get("tipo") == "efectivo" and tipo != "dividendo":
        errores.append(f"«{p.get('corto') or p['nombre']}» es una cuenta: se sigue con saldos (en «Saldos y "
                       "valores»). Como movimiento solo se le pueden anotar los intereses que te paga.")
    elif p and p.get("tipo") in SOLO_SALDO and p.get("tipo") != "efectivo":
        errores.append(f"«{p.get('corto') or p['nombre']}» se sigue con saldos, no con movimientos: "
                       "anótalo en «Saldos y valores».")
    if tipo not in TIPOS_MOV:
        errores.append("Elige el tipo de movimiento: compra, venta, dividendo o comisión.")
    f = fecha(datos.get("fecha"), errores)
    importe = numero(datos.get("importe"), "el importe", errores, mayor_que=0)
    cotiza = bool(p and p.get("fuente") != "manual")
    unidades = 0.0
    if tipo == "compra":
        unidades = numero(datos.get("unidades"), "cuántas unidades compraste", errores,
                          obligatorio=cotiza, mayor_que=0 if cotiza else None) or 0.0
    elif tipo == "venta":
        unidades = numero(datos.get("unidades"), "cuántas unidades vendiste", errores,
                          obligatorio=cotiza, minimo=0) or 0.0
    comision = numero(datos.get("comision"), "la comisión", errores, obligatorio=False, minimo=0)
    if comision and importe and comision >= importe:
        errores.append("La comisión no puede ser mayor que el importe total.")
    if not errores and tipo == "venta" and cotiza and unidades:
        tenias = unidades_a(cfg, p["id"], f, excluir=datos.get("id"))
        if unidades > tenias and unidades - tenias <= max(1e-4, tenias * 1e-6):
            unidades = tenias   # lo vendes todo: los últimos decimales no se pueden escribir exactos
        elif unidades > tenias + 1e-9:
            errores.append(f"El {fmt_fecha(f)} solo tenías {fmt_unidades(tenias)} unidades de "
                           f"«{p.get('corto') or p['nombre']}» y estás vendiendo {fmt_unidades(unidades)}. "
                           "Para venderlo todo, pulsa «Todas».")
    if errores:
        raise ErrorValidacion(errores)

    mov = {"fecha": f, "producto": p["id"], "tipo": tipo, "unidades": round(unidades, 8),
           "importe": round(importe, 2)}
    if comision:
        mov["comision"] = round(comision, 2)
    if texto(datos.get("nota")):
        mov["nota"] = texto(datos.get("nota"))
    lista = cfg.setdefault("movimientos", [])
    existente = next((m for m in lista if m.get("id") == datos.get("id")), None) if datos.get("id") else None
    if existente:
        existente.clear()
        existente.update(id=datos["id"], **mov)
        return existente
    mov = {"id": siguiente_id(lista, "m"), **mov}
    lista.append(mov)
    lista.sort(key=lambda m: m["fecha"])
    return mov


def guarda_traspaso(cfg, datos):
    """
    Traspaso de un fondo (o plan de pensiones) a otro. No es una venta: el coste y la
    antigüedad de lo que sale pasan al destino. Se guarda como un solo movimiento del
    origen, con el destino y las participaciones que salen y entran.
    """
    errores = []
    origen, destino = producto(cfg, datos.get("producto")), producto(cfg, datos.get("destino"))
    if not origen:
        errores.append("Elige de qué producto sale el dinero.")
    if not destino:
        errores.append("Elige a qué producto va.")
    if origen and destino and origen["id"] == destino["id"]:
        errores.append("El origen y el destino son el mismo producto.")
    for p in (origen, destino):
        if p and p.get("tipo") in SOLO_SALDO:
            errores.append(f"«{p.get('corto') or p['nombre']}» es una cuenta o una deuda: no se puede traspasar. "
                           "Un traspaso es entre fondos, planes de pensiones u otros productos de inversión.")
    f = fecha(datos.get("fecha"), errores)
    importe = numero(datos.get("importe"), "el importe traspasado", errores, mayor_que=0)
    cotiza_o = bool(origen and origen.get("fuente") != "manual")
    cotiza_d = bool(destino and destino.get("fuente") != "manual")
    unidades = numero(datos.get("unidades"), "cuántas participaciones salen", errores,
                      obligatorio=cotiza_o, minimo=0) or 0.0
    if cotiza_o and not unidades and not errores:
        errores.append("Faltan las participaciones que salen. Para traspasarlo todo, pulsa «Todas».")
    unidades_d = numero(datos.get("unidadesDestino"), "cuántas participaciones entran", errores,
                        obligatorio=cotiza_d, minimo=0) or 0.0
    if cotiza_d and not unidades_d and not errores:
        errores.append("Faltan las participaciones que entran en el destino.")
    total = bool(datos.get("total"))
    if not errores and cotiza_o:
        tenias = unidades_a(cfg, origen["id"], f, excluir=datos.get("id"))
        if unidades > tenias and unidades - tenias <= max(1e-4, tenias * 1e-6):
            unidades = tenias
        elif unidades > tenias + 1e-9:
            errores.append(f"El {fmt_fecha(f)} solo tenías {fmt_unidades(tenias)} participaciones de "
                           f"«{origen.get('corto') or origen['nombre']}» y estás traspasando {fmt_unidades(unidades)}.")
        total = abs(unidades - tenias) <= 1e-9
    if errores:
        raise ErrorValidacion(errores)

    mov = {"fecha": f, "producto": origen["id"], "destino": destino["id"], "tipo": "traspaso",
           "unidades": round(unidades, 8), "unidadesDestino": round(unidades_d, 8), "importe": round(importe, 2)}
    if total:
        mov["total"] = True
    if texto(datos.get("nota")):
        mov["nota"] = texto(datos.get("nota"))
    lista = cfg.setdefault("movimientos", [])
    existente = next((m for m in lista if m.get("id") == datos.get("id")), None) if datos.get("id") else None
    if existente:
        existente.clear()
        existente.update(id=datos["id"], **mov)
        return existente
    mov = {"id": siguiente_id(lista, "m"), **mov}
    lista.append(mov)
    lista.sort(key=lambda m: m["fecha"])
    return mov


def guarda_ajuste(cfg, datos):
    """
    «Según mi banco tengo X participaciones»: corrige las que calcula Rumbo (por ejemplo,
    tras importar traspasos de MyInvestor) sin tocar lo que pagaste. Desde esa fecha el
    producto tiene exactamente esas participaciones.
    """
    errores = []
    p = producto(cfg, datos.get("producto"))
    if not p:
        errores.append("Elige el producto.")
    elif p.get("fuente") == "manual" or p.get("tipo") in SOLO_SALDO:
        errores.append(f"«{p.get('corto') or p['nombre']}» se valora a mano: no tiene participaciones que cuadrar.")
    f = fecha(datos.get("fecha"), errores)
    unidades = numero(datos.get("unidades"), "cuántas participaciones tienes según tu banco", errores, minimo=0)
    if errores:
        raise ErrorValidacion(errores)
    mov = {"fecha": f, "producto": p["id"], "tipo": "ajuste", "unidades": round(unidades, 8), "importe": 0}
    nota = texto(datos.get("nota"))
    calculadas = unidades_a(cfg, p["id"], f, excluir=datos.get("id"))
    mov["nota"] = nota or f"Rumbo calculaba {fmt_unidades(calculadas)}"
    lista = cfg.setdefault("movimientos", [])
    existente = next((m for m in lista if m.get("id") == datos.get("id")), None) if datos.get("id") else None
    if existente:
        existente.clear()
        existente.update(id=datos["id"], **mov)
        return existente
    mov = {"id": siguiente_id(lista, "m"), **mov}
    lista.append(mov)
    lista.sort(key=lambda m: m["fecha"])
    return mov


def borra_movimiento(cfg, mid):
    antes = len(cfg.get("movimientos", []))
    cfg["movimientos"] = [m for m in cfg.get("movimientos", []) if m.get("id") != mid]
    if len(cfg["movimientos"]) == antes:
        raise ErrorValidacion(["Ese movimiento ya no existe."])


# ---------------------------------------------------------------- valores anotados

def guarda_valoracion(cfg, datos):
    errores = []
    p = producto(cfg, datos.get("producto"))
    if not p:
        errores.append("Elige el producto.")
    f = fecha(datos.get("fecha"), errores)
    valor = numero(datos.get("valor"), "el valor", errores, minimo=0)
    aportado = numero(datos.get("aportado"), "lo aportado", errores, obligatorio=False, minimo=0)
    if errores:
        raise ErrorValidacion(errores)
    val = {"fecha": f, "producto": p["id"], "valor": round(valor, 2)}
    if aportado is not None and p.get("tipo") not in SOLO_SALDO:
        val["aportado"] = round(aportado, 2)
    lista = cfg.setdefault("valoraciones", [])
    # Un solo valor por producto y día: si ya había uno, se sustituye.
    existente = next((v for v in lista if v.get("id") == datos.get("id")), None) if datos.get("id") else None
    existente = existente or next((v for v in lista if v["producto"] == p["id"] and v["fecha"] == f), None)
    if existente:
        vid = existente["id"]
        existente.clear()
        existente.update(id=vid, **val)
        return existente
    val = {"id": siguiente_id(lista, "v"), **val}
    lista.append(val)
    lista.sort(key=lambda v: (v["producto"], v["fecha"]))
    return val


def borra_valoracion(cfg, vid):
    antes = len(cfg.get("valoraciones", []))
    cfg["valoraciones"] = [v for v in cfg.get("valoraciones", []) if v.get("id") != vid]
    if len(cfg["valoraciones"]) == antes:
        raise ErrorValidacion(["Ese valor ya no existe."])
