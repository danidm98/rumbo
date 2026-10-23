# -*- coding: utf-8 -*-
"""
fiscal.py  ·  Resumen para la declaración de la Renta (orientativo)
===================================================================
Lo que hace falta para repasar el borrador de un año:

- cada venta, con su valor de transmisión (lo cobrado, ya sin comisión), su valor de
  adquisición (lo que te costó, con la comisión de compra) y la fecha de las compras de
  las que sale. Se usa el mismo FIFO que el panel (lo primero que compraste es lo primero
  que vendes), y los traspasos entre fondos conservan el coste y la fecha de la compra;
- los dividendos, cupones e intereses cobrados, tal y como los anotaste (normalmente, lo que
  te ingresaron, ya sin retención: en la Renta va el bruto y la retención aparte);
- las comisiones que pagaste aparte de una compra o una venta;
- los rescates de planes de pensiones, aparte: no son una ganancia patrimonial.

Y avisa de lo que conviene revisar a mano (la regla de los dos meses, traspasos que no
son entre fondos, ventas sin coste conocido). No es asesoramiento fiscal: Rumbo no sabe
las retenciones ni tu situación, y Hacienda ya tiene casi todo esto en tus datos fiscales.
No sale nada a internet: se calcula con tus movimientos.
"""

import datetime as dt
import re
from collections import defaultdict

from . import motor

# Tipos en los que se mira la regla de los dos meses (o de un año, en fondos no cotizados).
VENTANA_RECOMPRA = {"etf": 2, "accion": 2, "fondo": 12}
ES_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def r2(x):
    return round(x + 0.0, 2)


def suma_meses(fecha, meses):
    """fecha (AAAA-MM-DD) más 'meses' (negativo para restar), sin pasarse de fin de mes."""
    d = dt.date.fromisoformat(fecha)
    m = d.month - 1 + meses
    ano, mes = d.year + m // 12, m % 12 + 1
    for dia in (d.day, 30, 29, 28):
        try:
            return dt.date(ano, mes, dia).isoformat()
        except ValueError:
            continue


def isin_de(p):
    """El ISIN del producto: el suyo o, si no, su identificador cuando lo es."""
    for k in ("isin", "identificador"):
        x = str(p.get(k) or "").strip().upper()
        if ES_ISIN.match(x):
            return x
    return ""


def anos_con_datos(cfg):
    return sorted({m["fecha"][:4] for m in cfg.get("movimientos", [])
                   if m.get("tipo") in ("venta", "dividendo", "comision", "traspaso") and m.get("fecha")},
                  reverse=True)


def ano_por_defecto(anos, hoy=None):
    """El último año ya cerrado con datos (el de la Renta que toca); si no hay, el más reciente."""
    actual = str((hoy or dt.date.today()).year)
    cerrados = [a for a in anos if a < actual]
    return cerrados[0] if cerrados else (anos[0] if anos else actual)


def informe(cfg, ano=None, hoy=None, cache=None):
    """'cache': la carpeta de precios de Yahoo, de la que salen los splits (por defecto, la del motor)."""
    anos = anos_con_datos(cfg)
    ano = str(ano) if ano else ano_por_defecto(anos, hoy)
    productos = {p["id"]: p for p in cfg.get("productos", [])}
    est, _, _ = motor.fifo_cartera(cfg, cache=cache)
    movs = cfg.get("movimientos", [])
    avisos = []

    def nombre(pid):
        p = productos.get(pid) or {}
        return motor.sin_html(p.get("nombre") or p.get("corto") or pid)

    compras_de = defaultdict(list)
    for m in movs:
        if m.get("tipo") == "compra":
            compras_de[m.get("producto")].append(m["fecha"])

    ventas, rescates = [], []
    for pid, e in est.items():
        p = productos.get(pid) or {}
        tipo = p.get("tipo") or "otro"
        for v in e.get("ventasDetalle", []):
            if v["fecha"][:4] != ano:
                continue
            compras = [{"fecha": f, "unidades": round(u, 6), "coste": r2(c)} for f, u, c in v["compras"]]
            linea = {"producto": pid, "nombre": nombre(pid), "isin": isin_de(p),
                     "tipo": motor.TIPOS.get(tipo, "Otro"), "fecha": v["fecha"],
                     "unidades": round(v["unidades"] + v["faltan"], 6) if v["unidades"] + v["faltan"] else None,
                     "transmision": r2(v["importe"]), "adquisicion": r2(v["coste"]),
                     "ganancia": r2(v["importe"] - v["coste"]), "compras": compras, "avisos": []}
            if tipo == "pension":
                rescates.append(linea)
                continue
            if not compras:
                linea["adquisicion"] = None
                linea["avisos"].append(
                    "Solo está anotada la ganancia, sin participaciones ni coste (así da MyInvestor algunos "
                    "reembolsos): suma tal cual. Compruébala con el informe fiscal de tu banco." if not linea["unidades"]
                    else "No hay compras anotadas de las que salga esta venta: su ganancia es lo cobrado tal cual. "
                         "Revísala con el informe de tu banco.")
            elif v["faltan"]:
                linea["avisos"].append("Vendes más de lo que tenías anotado: falta alguna compra y la ganancia "
                                       "no es fiable.")
            if compras and any(c["coste"] <= 0 for c in compras):
                linea["avisos"].append("Parte de lo vendido no tiene coste anotado (viene de cuadrar participaciones "
                                       "o de un traspaso sin coste). Revisa su valor de adquisición.")
            meses = VENTANA_RECOMPRA.get(tipo)
            if meses and linea["ganancia"] < 0:
                desde, hasta = suma_meses(v["fecha"], -meses), suma_meses(v["fecha"], meses)
                if any(desde <= f <= hasta and f != v["fecha"] for f in compras_de[pid]):
                    plazo = "dos meses" if meses == 2 else "un año"
                    linea["avisos"].append(f"Compraste el mismo producto en el {plazo} anterior o posterior a esta "
                                           f"venta con pérdida: puede que esa pérdida no se pueda compensar todavía "
                                           f"(regla de los {plazo.replace('un año', 'doce meses')}). Revísalo.")
            ventas.append(linea)
    ventas.sort(key=lambda x: (x["fecha"], x["nombre"]))
    rescates.sort(key=lambda x: x["fecha"])

    cobros, comisiones, gastos_piso = {}, defaultdict(float), defaultdict(float)
    for m in movs:
        if m.get("fecha", "")[:4] != ano or m.get("producto") not in productos:
            continue
        imp = float(m.get("importe") or 0)
        tipo_p = productos[m["producto"]].get("tipo")
        if m.get("tipo") == "dividendo":
            clase = ("Intereses" if tipo_p in ("efectivo", "p2p") else "Alquileres" if tipo_p == "inmueble"
                     else "Dividendos y cupones")
            x = cobros.setdefault(m["producto"], {"producto": m["producto"], "nombre": nombre(m["producto"]),
                                                  "clase": clase, "cobros": 0, "importe": 0.0})
            x["cobros"] += 1
            x["importe"] += imp
        elif m.get("tipo") == "comision":
            (gastos_piso if tipo_p == "inmueble" else comisiones)[m["producto"]] += imp
        elif m.get("tipo") == "traspaso":
            o, d_ = productos.get(m["producto"]) or {}, productos.get(m.get("destino")) or {}
            ok = (o.get("tipo"), d_.get("tipo")) in (("fondo", "fondo"), ("pension", "pension"))
            if not ok:
                avisos.append(f"Traspaso del {motor.fmt_fecha_es(m['fecha'])} de {nombre(m['producto'])} a "
                              f"{nombre(m.get('destino'))}: sin pasar por Hacienda solo se puede traspasar entre fondos "
                              "de inversión o entre planes de pensiones. Si en realidad vendiste y compraste, "
                              "anótalo como una venta y una compra.")
    cobros = sorted(({**x, "importe": r2(x["importe"])} for x in cobros.values()), key=lambda x: -x["importe"])
    comisiones = [{"producto": pid, "nombre": nombre(pid), "importe": r2(v)}
                  for pid, v in sorted(comisiones.items(), key=lambda kv: -kv[1]) if v]
    gastos_piso = [{"producto": pid, "nombre": nombre(pid), "importe": r2(v)}
                   for pid, v in sorted(gastos_piso.items(), key=lambda kv: -kv[1]) if v]

    con_coste = [v for v in ventas if v["adquisicion"] is not None]
    totales = {
        "ganancias": r2(sum(v["ganancia"] for v in ventas if v["ganancia"] > 0)),
        "perdidas": r2(sum(v["ganancia"] for v in ventas if v["ganancia"] < 0)),
        "saldo": r2(sum(v["ganancia"] for v in ventas)),
        "transmision": r2(sum(v["transmision"] for v in ventas)),
        "adquisicion": r2(sum(v["adquisicion"] for v in con_coste)),
        "dividendos": r2(sum(x["importe"] for x in cobros if x["clase"] == "Dividendos y cupones")),
        "intereses": r2(sum(x["importe"] for x in cobros if x["clase"] == "Intereses")),
        "alquileres": r2(sum(x["importe"] for x in cobros if x["clase"] == "Alquileres")),
        "gastosAlquiler": r2(sum(x["importe"] for x in gastos_piso)),
        "comisiones": r2(sum(x["importe"] for x in comisiones)),
        "rescates": r2(sum(v["transmision"] for v in rescates)),
    }
    if any(x["clase"] == "Alquileres" for x in cobros) or gastos_piso:
        avisos.append("Los alquileres van en su propio apartado de la Renta (rendimientos del capital inmobiliario), con "
                      "sus gastos deducibles, la amortización y las reducciones que te correspondan: aquí solo tienes lo "
                      "cobrado y lo gastado que anotaste.")
    if any(x["clase"] != "Alquileres" for x in cobros):
        avisos.append("Dividendos, cupones e intereses: van como los anotaste, que normalmente es lo que te ingresaron, ya sin "
                      "retención. En la Renta va el importe bruto y, aparte, la retención: los dos están en el informe "
                      "fiscal de tu banco o bróker.")
    if rescates:
        avisos.append("Lo que rescatas de un plan de pensiones no es una ganancia patrimonial: tributa como "
                      "rendimiento del trabajo (todo lo que sacas, no solo lo ganado). Va aparte.")
    return {"ano": ano, "anos": anos, "ventas": ventas, "rescates": rescates, "cobros": cobros,
            "comisiones": comisiones, "gastosAlquiler": gastos_piso, "totales": totales, "avisos": avisos}


def csv_texto(inf):
    """El resumen en CSV (separado por ;, decimales con coma) para abrirlo con Excel."""
    import csv
    import io
    num = lambda x: "" if x is None else f"{x:.2f}".replace(".", ",")
    fecha = motor.fmt_fecha_es
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow([f"Resumen para la Renta {inf['ano']} (orientativo, hecho con Rumbo: compruébalo con tu banco)"])
    w.writerow([])
    w.writerow(["Ventas", "ISIN", "Tipo", "Fecha de venta", "Fechas de compra", "Unidades",
                "Valor de transmisión", "Valor de adquisición", "Ganancia o pérdida", "Revisar"])
    for v in inf["ventas"]:
        fechas = sorted({c["fecha"] for c in v["compras"]})
        w.writerow([v["nombre"], v["isin"], v["tipo"], fecha(v["fecha"]), ", ".join(fecha(f) for f in fechas),
                    "" if v["unidades"] is None else f"{v['unidades']:.6f}".rstrip("0").rstrip(",.").replace(".", ","),
                    num(v["transmision"]), num(v["adquisicion"]), num(v["ganancia"]), " ".join(v["avisos"])])
    t = inf["totales"]
    w.writerow(["Total", "", "", "", "", "", num(t["transmision"]), num(t["adquisicion"]), num(t["saldo"]), ""])
    w.writerow([])
    w.writerow(["Dividendos, cupones e intereses", "Clase", "Cobros", "Importe anotado"])
    for x in inf["cobros"]:
        w.writerow([x["nombre"], x["clase"], x["cobros"], num(x["importe"])])
    if inf["comisiones"]:
        w.writerow([])
        w.writerow(["Comisiones pagadas aparte", "Importe"])
        for x in inf["comisiones"]:
            w.writerow([x["nombre"], num(x["importe"])])
    if inf["gastosAlquiler"]:
        w.writerow([])
        w.writerow(["Gastos de inmuebles anotados", "Importe"])
        for x in inf["gastosAlquiler"]:
            w.writerow([x["nombre"], num(x["importe"])])
    if inf["rescates"]:
        w.writerow([])
        w.writerow(["Rescates de planes de pensiones (rendimiento del trabajo)", "Fecha", "Importe"])
        for v in inf["rescates"]:
            w.writerow([v["nombre"], fecha(v["fecha"]), num(v["transmision"])])
    if inf["avisos"]:
        w.writerow([])
        for a in inf["avisos"]:
            w.writerow([a])
    return out.getvalue()
