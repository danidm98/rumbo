"""Internet simulado: descargas de Yahoo, reintentos y mensajes cuando falla la conexión."""
import contextlib
import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from comun import ConCarpeta, cartera
from app import buscar, importar, motor


class RespuestaFalsa(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ts(fecha):
    return int(time.mktime(time.strptime(fecha, "%Y-%m-%d"))) + 12 * 3600


def yahoo(fechas_cierres, granularidad="1d", splits=None):
    """splits: [(fecha, numerador, denominador)], como los manda Yahoo con events=split."""
    ts = [_ts(f) for f, _ in fechas_cierres]
    res = {"meta": {"dataGranularity": granularidad}, "timestamp": ts,
           "indicators": {"quote": [{"close": [c for _, c in fechas_cierres]}]}}
    if splits:
        res["events"] = {"splits": {str(_ts(f)): {"date": _ts(f), "numerator": n, "denominator": d,
                                                  "splitRatio": f"{n}:{d}"} for f, n, d in splits}}
    cuerpo = {"chart": {"result": [res]}}
    return RespuestaFalsa(json.dumps(cuerpo).encode())


class DescargaYahoo(ConCarpeta):
    def setUp(self):
        super().setUp()
        motor.CACHE = os.path.join(self.carpeta, "cache")
        motor.SIN_RED, motor.SOLO_FALTAN = False, False
        self.urls = []

    def descarga(self, respuesta, simbolo="AAPL"):
        def falso(req, timeout=None):
            self.urls.append(req.full_url)
            return respuesta
        with mock.patch("urllib.request.urlopen", falso):
            return motor.descargar_serie(simbolo)

    def test_pide_precios_diarios_por_fechas(self):
        # Regresión v1.1.2: con range=max Yahoo devolvía un precio al mes o al trimestre.
        self.descarga(yahoo([("2025-01-02", 10.0)]))
        self.assertIn("interval=1d", self.urls[0])
        self.assertIn("period1=", self.urls[0])
        self.assertNotIn("range=max", self.urls[0])

    def test_rechaza_precios_mensuales(self):
        serie = self.descarga(yahoo([("2025-01-01", 10.0), ("2025-02-01", 11.0)], granularidad="1mo"))
        self.assertEqual(serie, {})
        self.assertTrue(any("1mo" in a for a in motor.AVISOS))

    def test_limpia_la_cache_estropeada(self):
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "AAPL.json"), "w") as f:
            json.dump({"1999-01-04": 1.0, "2025-02-01": 999.0}, f)   # 2025-02-01 es sábado: dato mensual
        serie = self.descarga(yahoo([("2025-01-02", 10.0), ("2025-02-03", 11.0)]))
        self.assertEqual(serie, {"1999-01-04": 1.0, "2025-01-02": 10.0, "2025-02-03": 11.0})

    def test_pide_los_splits(self):
        self.descarga(yahoo([("2025-01-02", 10.0)]))
        self.assertIn("events=split", self.urls[0])

    def test_guarda_los_splits_aparte(self):
        self.descarga(yahoo([("2026-02-27", 3.3), ("2026-03-02", 3.4)],
                            splits=[("2026-03-02", 25, 1)]), simbolo="SPYI.DE")
        self.assertTrue(os.path.exists(os.path.join(motor.CACHE, "SPYI.DE.splits.json")))
        self.assertEqual(motor.splits_de("SPYI.DE"), {"2026-03-02": 25.0})
        self.assertEqual(motor.splits_de("SPYI.DE", cache=motor.CACHE), {"2026-03-02": 25.0})

    def test_sin_fichero_de_splits_no_hay_splits(self):
        self.assertEqual(motor.splits_de("AAPL"), {})
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "AAPL.splits.json"), "w") as f:
            f.write("{roto")
        self.assertEqual(motor.splits_de("AAPL"), {})

    def test_ignora_splits_mal_formados(self):
        self.descarga(yahoo([("2025-01-02", 10.0)], splits=[("2024-06-10", 10, 0)]))
        self.assertEqual(motor.splits_de("AAPL"), {})

    def test_un_split_nuevo_tira_la_cache_vieja(self):
        # Caso real SPYI.DE: Yahoo reajusta toda la serie tras el split y la cola vieja
        # (sin ajustar) saltaba de 83,06 a 3,32 el 2015-01-02.
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "SPYI.DE.json"), "w") as f:
            json.dump({"2014-12-30": 83.06, "2015-01-02": 83.5}, f)
        serie = self.descarga(yahoo([("2015-01-02", 3.32), ("2026-03-02", 3.4)],
                                    splits=[("2026-03-02", 25, 1)]), simbolo="SPYI.DE")
        self.assertEqual(serie, {"2015-01-02": 3.32, "2026-03-02": 3.4})
        with open(os.path.join(motor.CACHE, "SPYI.DE.json")) as f:
            self.assertNotIn("2014-12-30", json.load(f))

    def test_un_split_ya_conocido_mantiene_la_cache(self):
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "SPYI.DE.json"), "w") as f:
            json.dump({"2014-12-30": 3.30, "2015-01-02": 3.32}, f)
        with open(os.path.join(motor.CACHE, "SPYI.DE.splits.json"), "w") as f:
            json.dump({"2026-03-02": 25.0}, f)
        serie = self.descarga(yahoo([("2015-01-02", 3.32), ("2026-03-02", 3.4)],
                                    splits=[("2026-03-02", 25, 1)]), simbolo="SPYI.DE")
        self.assertEqual(serie, {"2014-12-30": 3.30, "2015-01-02": 3.32, "2026-03-02": 3.4})

    def guarda_cache(self, simbolo, serie):
        os.makedirs(motor.CACHE, exist_ok=True)
        with open(os.path.join(motor.CACHE, simbolo + ".json"), "w") as f:
            json.dump(serie, f)

    def test_fechas_comunes_que_no_cuadran_tiran_la_cache_vieja(self):
        # Yahoo reajusta el histórico sin avisar del split: las fechas que ya estaban
        # guardadas vuelven 25 veces más bajas, y lo anterior también está sin ajustar.
        self.guarda_cache("SPYI.DE", {"2014-12-30": 83.06, "2025-06-02": 250.0, "2025-06-03": 252.5})
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            serie = self.descarga(yahoo([("2025-06-02", 10.0), ("2025-06-03", 10.1)]), simbolo="SPYI.DE")
        self.assertEqual(serie, {"2025-06-02": 10.0, "2025-06-03": 10.1})
        self.assertIn("reajustado", salida.getvalue())

    def test_salto_de_split_en_la_union_tira_la_cache_vieja(self):
        # Caso real SPYI.DE: Yahoo no devuelve el evento del split (25:1) pero sí los
        # cierres ajustados; la descarga empieza el 2015-01-02 y la cola guardada no.
        self.guarda_cache("SPYI.DE", {"2014-12-29": 82.9, "2014-12-30": 83.06})
        serie = self.descarga(yahoo([("2015-01-02", 3.3172), ("2015-01-05", 3.30)]), simbolo="SPYI.DE")
        self.assertEqual(serie, {"2015-01-02": 3.3172, "2015-01-05": 3.30})

    def test_fechas_comunes_que_cuadran_mantienen_la_cache(self):
        self.guarda_cache("AAPL", {"2024-12-30": 9.9, "2025-01-02": 10.0, "2025-01-03": 10.2})
        serie = self.descarga(yahoo([("2025-01-02", 10.05), ("2025-01-03", 10.15), ("2025-01-06", 10.3)]))
        self.assertEqual(serie, {"2024-12-30": 9.9, "2025-01-02": 10.05, "2025-01-03": 10.15,
                                 "2025-01-06": 10.3})

    def test_una_descarga_corta_no_olvida_splits_antiguos(self):
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "SPYI.DE.splits.json"), "w") as f:
            json.dump({"2015-06-01": 2.0}, f)
        self.descarga(yahoo([("2026-03-02", 3.4)], splits=[("2026-03-02", 25, 1)]), simbolo="SPYI.DE")
        self.assertEqual(motor.splits_de("SPYI.DE"), {"2015-06-01": 2.0, "2026-03-02": 25.0})

    def test_sin_conexion_usa_la_cache(self):
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "AAPL.json"), "w") as f:
            json.dump({"2025-01-02": 10.0}, f)
        def caido(*a, **k):
            raise urllib.error.URLError("sin red")
        with mock.patch("urllib.request.urlopen", caido):
            serie = motor.descargar_serie("AAPL")
        self.assertEqual(serie, {"2025-01-02": 10.0})
        self.assertTrue(any("sin conexion" in a for a in motor.AVISOS))

    def test_solo_faltan_no_descarga_si_hay_cache(self):
        os.makedirs(motor.CACHE)
        with open(os.path.join(motor.CACHE, "AAPL.json"), "w") as f:
            json.dump({"2025-01-02": 10.0}, f)
        motor.SOLO_FALTAN = True
        self.assertEqual(self.descarga(yahoo([("2025-01-03", 11.0)])), {"2025-01-02": 10.0})
        self.assertEqual(self.urls, [])


class Reintentos(unittest.TestCase):
    def setUp(self):
        self.dormir = mock.patch("time.sleep", lambda s: None)
        self.dormir.start()

    def tearDown(self):
        self.dormir.stop()

    def test_tres_intentos_y_queda_registrado(self):
        llamadas = []
        def caido(*a, **k):
            llamadas.append(1)
            raise urllib.error.URLError("timeout")
        n = len(buscar.FALLOS)
        with mock.patch("urllib.request.urlopen", caido), self.assertRaises(urllib.error.URLError):
            buscar._get("https://query2.finance.yahoo.com/v1/finance/search?q=x")
        self.assertEqual(len(llamadas), 3)
        self.assertIn("query2.finance.yahoo.com", buscar.hubo_fallos_desde(n))

    def test_un_404_no_se_reintenta(self):
        llamadas = []
        def no_existe(req, timeout=None):
            llamadas.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        with mock.patch("urllib.request.urlopen", no_existe), self.assertRaises(urllib.error.HTTPError):
            buscar._get("https://query1.finance.yahoo.com/v8/finance/chart/NOEXISTE")
        self.assertEqual(len(llamadas), 1)

    def test_demasiadas_peticiones_si_se_reintenta(self):
        respuestas = [urllib.error.HTTPError("u", 429, "Too Many", {}, None), RespuestaFalsa(b'{"ok": 1}')]
        def a_veces(req, timeout=None):
            r = respuestas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("urllib.request.urlopen", a_veces):
            self.assertEqual(buscar._get("https://query2.finance.yahoo.com/x"), {"ok": 1})


class MensajesDelImportador(ConCarpeta):
    def resolver(self, simula):
        plan = importar.Plan()
        with mock.patch.object(buscar, "buscar", simula):
            importar.Resolutor(cartera(), plan, self.carpeta).producto(7, "IE00BYX5NX33", "", "fondo")
        return plan.errores[0]["mensaje"]

    def test_sin_conexion_no_dice_que_no_existe(self):
        def caido(texto):
            buscar.FALLOS.append("lt.morningstar.com: timeout")
            return []
        self.assertIn("No he podido consultar", self.resolver(caido))

    def test_no_existe_de_verdad(self):
        self.assertIn("No encuentro «IE00BYX5NX33»", self.resolver(lambda texto: []))


if __name__ == "__main__":
    unittest.main()
