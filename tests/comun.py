"""
Ayudas comunes de los tests.

- Cortan internet para todo el proceso: si algo intenta descargar, el test falla.
- Dan precios inventados (deterministas) en lugar de Yahoo, Morningstar o CoinGecko.
- Crean carteras y carpetas de datos temporales, sin tocar nunca mis_datos.
"""
import copy
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

# Red de seguridad: si algo se escapa de los parches (un hilo que acaba tarde, una prueba
# que olvida cambiar la carpeta), escribe en una carpeta temporal, nunca en mis_datos.
os.environ["PATRIMONIO_DATOS"] = tempfile.mkdtemp(prefix="rumbo_tests_")

from app import almacen, motor  # noqa: E402


class SinInternet(Exception):
    """Algo ha intentado salir a internet durante los tests."""


def _prohibido(*args, **kwargs):
    url = getattr(args[0], "full_url", args[0]) if args else "?"
    raise SinInternet(f"Los tests no pueden salir a internet ({url})")


urllib.request.urlopen = _prohibido


# ---------------------------------------------------------------- la app con su llave

from flask.testing import FlaskClient  # noqa: E402

from app import servidor  # noqa: E402


class ClienteConLlave(FlaskClient):
    """Como el navegador que abrió el lanzador: manda la cookie con la llave de acceso.
    Para probar lo que pasa SIN llave, usar FlaskClient(servidor.app) directamente."""

    def open(self, *args, **kwargs):
        # La llave depende de la carpeta de datos (cada test usa la suya): se pone en cada petición.
        dominios = {"localhost", "127.0.0.1", "::1", "rumbo.casa"}
        cabeceras = kwargs.get("headers")
        if isinstance(cabeceras, dict) and cabeceras.get("Host"):   # otra dirección (Docker en la red de casa)
            dominios.add(servidor._sin_puerto(cabeceras["Host"]).strip("[]"))
        for dominio in dominios:
            self.set_cookie(servidor.COOKIE, servidor.token_acceso(), domain=dominio)
        return super().open(*args, **kwargs)


servidor.app.test_client_class = ClienteConLlave


# ---------------------------------------------------------------- precios inventados

def dias(desde, hasta=None, finde=False):
    d, fin = dt.date.fromisoformat(desde), dt.date.fromisoformat(hasta) if hasta else dt.date.today()
    while d <= fin:
        if finde or d.weekday() < 5:
            yield d
        d += dt.timedelta(days=1)


def serie_inventada(base=10.0, desde="2015-01-02", hasta=None, paso=0.0003):
    """Precio que sube un poco cada día hábil: base * (1 + paso) ** n."""
    return {d.isoformat(): round(base * (1 + paso) ** i, 6) for i, d in enumerate(dias(desde, hasta))}


class PreciosInventados:
    """
    Sustituye las descargas del motor por series fijas. Lo que no se indique en
    'series' se inventa a partir del nombre del símbolo (siempre el mismo valor).
    Uso:  with PreciosInventados({"AAPL": {...}}): motor.construir(...)
    """

    def __init__(self, series=None):
        self.series = dict(series or {})
        self.pedidos = []

    def serie(self, clave):
        self.pedidos.append(clave)
        if clave in self.series:
            return dict(self.series[clave])
        if clave.endswith(".F"):          # línea secundaria de Yahoo de los fondos: no hace falta
            return {}
        if clave.endswith("EUR=X"):       # cambio de divisa: fijo
            return serie_inventada(base=0.9, paso=0)
        h = int(hashlib.md5(clave.encode()).hexdigest()[:6], 16)
        return serie_inventada(base=5 + h % 200, paso=0.0002)

    def __enter__(self):
        self._parches = [
            mock.patch.object(motor, "descargar_serie", lambda s, anos=None: self.serie(s)),
            mock.patch.object(motor, "descargar_morningstar", lambda s, *a, **k: self.serie("MS:" + s)),
            mock.patch.object(motor, "descargar_coingecko", lambda s, *a, **k: self.serie("CG:" + s)),
        ]
        for p in self._parches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._parches:
            p.stop()


# ---------------------------------------------------------------- carteras

def cartera(productos=(), movimientos=(), valoraciones=()):
    cfg = copy.deepcopy(almacen.CARTERA_VACIA)
    cfg["productos"] = [dict(p) for p in productos]
    cfg["movimientos"] = [dict(m) for m in movimientos]
    cfg["valoraciones"] = [dict(v) for v in valoraciones]
    return cfg


def etf(pid="etf", codigo="TEST.DE", moneda="EUR", **extra):
    return {"id": pid, "nombre": pid.upper(), "tipo": "etf", "fuente": "yahoo", "codigo": codigo,
            "moneda": moneda, "slot": 1, "largoPlazo": True, **extra}


def manual(pid="piso", tipo="inmueble", **extra):
    return {"id": pid, "nombre": pid.capitalize(), "tipo": tipo, "fuente": "manual", "slot": 2,
            "largoPlazo": tipo not in almacen.SOLO_SALDO, **extra}


def mov(fecha, producto, tipo, unidades, importe, mid=None, **extra):
    return {"id": mid or f"m{abs(hash((fecha, producto, tipo, unidades, importe))) % 10**8}",
            "fecha": fecha, "producto": producto, "tipo": tipo, "unidades": unidades,
            "importe": importe, **extra}


def guarda_cache_yahoo(carpeta, simbolo, serie=None, splits=None):
    """Deja en <carpeta>/cache lo que guardaría Yahoo de un símbolo: sus cierres (ya ajustados
    a los splits) y sus splits ({fecha: proporción}). Devuelve la carpeta de la cache."""
    cache = os.path.join(carpeta, "cache")
    os.makedirs(cache, exist_ok=True)
    base = motor._base_cache(simbolo, cache)
    for ext, datos in ((".json", serie), (".splits.json", splits)):
        if datos is not None:
            with open(base + ext, "w", encoding="utf-8") as f:
                json.dump(datos, f)
    return cache


def lee_demo():
    with open(os.path.join(RAIZ, "demo", "cartera.json"), encoding="utf-8") as f:
        return json.load(f)


class ConCarpeta(unittest.TestCase):
    """Test con una carpeta de datos temporal (se borra al acabar)."""

    def setUp(self):
        self.carpeta = tempfile.mkdtemp(prefix="rumbo_test_")
        motor.AVISOS.clear()

    def tearDown(self):
        shutil.rmtree(self.carpeta, ignore_errors=True)
