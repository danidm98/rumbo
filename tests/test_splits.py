"""Splits de acciones y ETFs: Yahoo ajusta hacia atrás los precios, pero tus movimientos
guardan las participaciones que te dio el broker. El motor las pasa a participaciones de
después del split al calcular (sin tocar cartera.json) y no ajusta dos veces si ya lo hiciste tú."""
import copy
import os
import unittest
from unittest import mock

from comun import ConCarpeta, PreciosInventados, cartera, etf, guarda_cache_yahoo, mov, serie_inventada
from app import almacen, fiscal, motor, servidor

SPLIT = "2025-03-03"            # ex-date: ese día ya cotiza con las participaciones nuevas
SPLITS = {SPLIT: 4.0}           # 4 nuevas por cada 1 antigua
# Cierres como los da Yahoo: ajustados al split (antes del split el precio real era 4 veces este).
SERIE = serie_inventada(base=25, desde="2024-01-01", paso=0.0003)
SERIE_B = serie_inventada(base=20, desde="2024-01-01", paso=0)

ANTES, DESPUES, VENTA = "2024-01-02", "2025-06-02", "2025-09-01"
IMP_ANTES = round(2 * 4 * SERIE[ANTES], 2)        # 2 participaciones antiguas a su precio real
IMP_DESPUES = round(8 * SERIE[DESPUES], 2)        # 8 participaciones nuevas
IMP_VENTA = round(16 * SERIE[VENTA], 2)


def compras(unidades_antes):
    return [mov(ANTES, "etf", "compra", unidades_antes, IMP_ANTES, mid="c1"),
            mov(DESPUES, "etf", "compra", 8, IMP_DESPUES, mid="c2")]


class ConSplit(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.cache = guarda_cache_yahoo(self.carpeta, "TEST.DE", SERIE, SPLITS)

    def construye(self, cfg):
        with PreciosInventados({"TEST.DE": SERIE, "OTRO.DE": SERIE_B}):
            return motor.construir(cfg, self.carpeta, descargar=False)


class AjustaSplits(ConSplit):
    """La función que pasa las participaciones de antes del split a las de después."""

    def ajusta(self, movs, **kw):
        return motor.ajusta_splits(movs, [etf()], cache=self.cache, **kw)

    def test_compra_antes_del_split_en_participaciones_del_broker(self):
        movs = compras(2)
        nuevos, resumen = self.ajusta(movs)
        self.assertAlmostEqual(nuevos[0]["unidades"], 8)
        self.assertAlmostEqual(nuevos[1]["unidades"], 8)
        self.assertEqual(movs[0]["unidades"], 2)          # el original no se toca
        self.assertEqual(resumen["etf"]["movimientos"], 1)
        self.assertEqual(resumen["etf"]["splits"], {SPLIT: 4.0})

    def test_ya_corregida_a_mano_no_se_ajusta_dos_veces(self):
        nuevos, resumen = self.ajusta(compras(8))
        self.assertAlmostEqual(nuevos[0]["unidades"], 8)
        self.assertEqual(resumen, {})

    def test_el_dia_del_split_ya_es_despues(self):
        # Sin precio que mirar (un ajuste), manda la fecha del split de Yahoo: lo de ese
        # mismo día ya va en participaciones nuevas y lo del día anterior no.
        nuevos, _ = self.ajusta([mov(SPLIT, "etf", "ajuste", 1, 0), mov("2025-02-28", "etf", "ajuste", 1, 0)])
        self.assertAlmostEqual(nuevos[0]["unidades"], 1)
        self.assertAlmostEqual(nuevos[1]["unidades"], 4)

    def test_sin_importe_o_sin_precio_se_supone_del_broker(self):
        nuevos, _ = self.ajusta([mov("2024-06-03", "etf", "ajuste", 3, 0),
                                 mov("2023-01-02", "etf", "compra", 1, 100)])   # sin cierres antes de 2024
        self.assertAlmostEqual(nuevos[0]["unidades"], 12)
        # Sin cierre ese día se compara con el primero que hay (25 ≈ 100 / 4): se ajusta.
        self.assertAlmostEqual(nuevos[1]["unidades"], 4)

    def test_dividendos_y_comisiones_no_se_tocan(self):
        movs = [mov("2024-06-03", "etf", "dividendo", 0, 5), mov("2024-06-03", "etf", "comision", 0, 1)]
        nuevos, resumen = self.ajusta(movs)
        self.assertEqual(nuevos, movs)
        self.assertEqual(resumen, {})

    def test_hasta_una_fecha_da_las_participaciones_de_ese_dia(self):
        nuevos, _ = self.ajusta(compras(2)[:1], hasta="2024-06-03")
        self.assertAlmostEqual(nuevos[0]["unidades"], 2)
        nuevos, _ = self.ajusta(compras(8)[:1], hasta="2024-06-03")   # corregida a mano: vuelve a las del broker
        self.assertAlmostEqual(nuevos[0]["unidades"], 2)

    def test_sin_fichero_de_splits_no_cambia_nada(self):
        movs = [mov(ANTES, "x", "compra", 2, 9999)]
        nuevos, resumen = motor.ajusta_splits(movs, [etf("x", "SINSPLITS.DE")], cache=self.cache)
        self.assertEqual(nuevos, movs)
        self.assertEqual(resumen, {})

    def test_solo_productos_de_yahoo(self):
        p = {**etf(), "fuente": "morningstar"}
        nuevos, _ = motor.ajusta_splits(compras(2), [p], cache=self.cache)
        self.assertEqual(nuevos[0]["unidades"], 2)


class Construir(ConSplit):
    def test_participaciones_valor_y_rentabilidad(self):
        D = self.construye(cartera([etf()], compras(2)))
        p = D["productos"][0]
        self.assertAlmostEqual(p["participaciones"], 16)
        self.assertAlmostEqual(p["valor"], 16 * SERIE[p["navFecha"]], places=2)
        self.assertAlmostEqual(p["aportado"], IMP_ANTES + IMP_DESPUES, places=2)
        rent = D["rentabilidadAnual"]
        for ano, r in zip(rent["anos"], rent["porProducto"]["etf"]):
            with self.subTest(ano=ano):
                self.assertGreater(r, 0)          # sube un poco cada día: nada de -75 %
                self.assertLess(r, 0.15)
        self.assertEqual(p["aportaciones"][0]["participaciones"], 8)

    def test_corregida_a_mano_da_lo_mismo(self):
        D1 = self.construye(cartera([etf()], compras(2)))
        D2 = self.construye(cartera([etf()], compras(8)))
        p1, p2 = D1["productos"][0], D2["productos"][0]
        for k in ("participaciones", "valor", "aportado", "plusvalia"):
            with self.subTest(campo=k):
                self.assertAlmostEqual(p1[k], p2[k], places=6)
        self.assertEqual(D1["rentabilidadAnual"]["porProducto"], D2["rentabilidadAnual"]["porProducto"])
        self.assertFalse(any("split" in a for a in D2["avisos"]))

    def test_avisa_de_los_movimientos_ajustados(self):
        D = self.construye(cartera([etf()], compras(2)))
        avisos = [a for a in D["avisos"] if "split" in a]
        self.assertEqual(len(avisos), 1, D["avisos"])
        self.assertIn("split de 4 por 1 el 03/03/2025", avisos[0])
        self.assertIn("1 movimiento anterior", avisos[0])

    def test_venta_de_todo_despues_del_split(self):
        cfg = cartera([etf()], compras(2) + [mov(VENTA, "etf", "venta", 16, IMP_VENTA, mid="v1")])
        D = self.construye(cfg)
        p = D["productos"][0]
        self.assertAlmostEqual(p["participaciones"], 0)
        self.assertAlmostEqual(p["realizado"], IMP_VENTA - IMP_ANTES - IMP_DESPUES, places=2)
        self.assertFalse(any("vendes más" in a for a in D["avisos"]), D["avisos"])

    def test_no_reescribe_la_cartera(self):
        cfg = cartera([etf()], compras(2))
        antes = copy.deepcopy(cfg)
        self.construye(cfg)
        self.assertEqual(cfg, antes)

    def test_traspaso_hacia_un_producto_con_split(self):
        # De OTRO.DE (sin split) a TEST.DE antes del split: entran participaciones antiguas.
        f = "2024-06-03"
        raw = round(200 / (4 * SERIE[f]), 6)
        for unidades_destino, esperadas in ((raw, 4 * raw), (4 * raw, 4 * raw)):   # del broker y corregidas
            with self.subTest(unidades_destino=unidades_destino):
                cfg = cartera([etf(), etf("b", "OTRO.DE")], [
                    mov(ANTES, "b", "compra", 10, 200, mid="cb"),
                    {"id": "t1", "fecha": f, "producto": "b", "destino": "etf", "tipo": "traspaso",
                     "unidades": 10, "unidadesDestino": unidades_destino, "importe": 200}])
                est, _, _ = motor.fifo_cartera(cfg, cache=self.cache)
                self.assertAlmostEqual(sum(e[1] for e in est["etf"]["eventos"]), esperadas, places=5)
                self.assertAlmostEqual(sum(e[1] for e in est["b"]["eventos"]), 0)
                self.assertAlmostEqual(almacen.unidades_a(cfg, "etf", DESPUES, cache=self.cache), esperadas, places=5)
                self.assertAlmostEqual(almacen.unidades_a(cfg, "etf", f, cache=self.cache), raw, places=5)

    def test_traspaso_de_todo_despues_del_split(self):
        cfg = cartera([etf(), etf("b", "OTRO.DE")], compras(2) + [
            {"id": "t1", "fecha": VENTA, "producto": "etf", "destino": "b", "tipo": "traspaso",
             "unidades": 16, "unidadesDestino": IMP_VENTA / 20, "importe": IMP_VENTA}])
        D = self.construye(cfg)
        self.assertFalse(any("traspasas más" in a for a in D["avisos"]), D["avisos"])
        b = next(p for p in D["productos"] if p["id"] == "b")
        self.assertAlmostEqual(b["aportado"], IMP_ANTES + IMP_DESPUES, places=2)   # el coste pasa entero

    def test_lotes_vivos(self):
        lotes = motor.lotes_vivos(cartera([etf()], compras(2)), "etf", DESPUES, cache=self.cache)
        self.assertEqual([round(l[0], 6) for l in lotes], [8, 8])
        lotes = motor.lotes_vivos(cartera([etf()], compras(2)), "etf", "2024-06-03", cache=self.cache)
        self.assertEqual([round(l[0], 6) for l in lotes], [2])


class Renta(ConSplit):
    def setUp(self):
        super().setUp()
        self.cfg = cartera([etf()], compras(2) + [mov(VENTA, "etf", "venta", 16, IMP_VENTA, mid="v1")])

    def test_venta_despues_del_split(self):
        inf = fiscal.informe(self.cfg, "2025", cache=self.cache)
        v = inf["ventas"][0]
        self.assertAlmostEqual(v["unidades"], 16)
        self.assertEqual([c["unidades"] for c in v["compras"]], [8, 8])
        self.assertEqual([c["fecha"] for c in v["compras"]], [ANTES, DESPUES])
        self.assertAlmostEqual(v["adquisicion"], IMP_ANTES + IMP_DESPUES, places=2)
        self.assertAlmostEqual(v["ganancia"], IMP_VENTA - IMP_ANTES - IMP_DESPUES, places=2)
        self.assertEqual(v["avisos"], [])

    def test_en_la_app_con_la_cache_compartida(self):
        almacen.guarda(os.path.join(self.carpeta, "cartera.json"), self.cfg)
        with mock.patch.object(servidor, "DATOS", self.carpeta), mock.patch.object(motor, "CACHE", "no_existe"):
            web = servidor.app.test_client()
            j = web.get("/api/fiscal?ano=2025").get_json()
            self.assertAlmostEqual(j["ventas"][0]["unidades"], 16)
            j = web.get(f"/api/unidades?producto=etf&fecha={DESPUES}").get_json()
            self.assertAlmostEqual(j["unidades"], 16)


class VenderTodo(ConSplit):
    def test_unidades_a_despues_del_split(self):
        for antes in (2, 8):   # del broker y corregidas a mano
            with self.subTest(antes=antes):
                cfg = cartera([etf()], compras(antes))
                self.assertAlmostEqual(almacen.unidades_a(cfg, "etf", DESPUES, cache=self.cache), 16)
                self.assertAlmostEqual(almacen.unidades_a(cfg, "etf", "2024-06-03", cache=self.cache), 2)

    def test_vender_todo_despues_del_split_se_valida_bien(self):
        cfg = cartera([etf()], compras(2))
        with mock.patch.object(motor, "CACHE", self.cache):
            m = almacen.guarda_movimiento(cfg, {"producto": "etf", "tipo": "venta", "fecha": VENTA,
                                                "unidades": "16", "importe": str(IMP_VENTA)})
        self.assertEqual(m["unidades"], 16)


# ---------------------------------------------------------------- split deducido del precio
# Como SPYI.DE: Yahoo da los cierres ajustados al split de 25 por 1 de febrero de 2026, pero
# no manda el evento. Las compras de antes guardan las participaciones del broker y salen a
# unas 25 veces el cierre ajustado (con las comisiones, entre 24,9 y 25,35).

SERIE_25 = serie_inventada(base=8, desde="2025-01-01", paso=0.0005)
ANTES_25 = [f"2025-{m:02d}-16" for m in range(4, 13)] + ["2026-01-16", "2026-02-16"]
DESPUES_25 = [f"2026-{m:02d}-16" for m in range(3, 7)]
RUIDO = [0.996, 1.004, 1.014, 0.998, 1.002, 1.0, 1.006, 0.999, 1.003, 1.001, 0.997]
SPYI = dict(nombre="State Street SPDR MSCI All Country", corto="Stt StrtSPDRMSCIAllCount")


def cierre(serie, fecha):
    return motor.valor_en(serie, fecha)


def compras_25(corregidas=False, pid="spyi"):
    """Once compras de 2 participaciones del broker (50 de después) y cuatro de 50 después del split."""
    movs = [mov(f, pid, "compra", 50 if corregidas else 2, round(2 * 25 * cierre(SERIE_25, f) * RUIDO[i], 2),
                mid=f"a{i}") for i, f in enumerate(ANTES_25)]
    movs += [mov(f, pid, "compra", 50, round(50 * cierre(SERIE_25, f) * RUIDO[i], 2), mid=f"d{i}")
             for i, f in enumerate(DESPUES_25)]
    return movs


class ConSplitSinEvento(ConCarpeta):
    def setUp(self):
        super().setUp()
        self.cache = guarda_cache_yahoo(self.carpeta, "SPYI.DE", SERIE_25)   # sin fichero de splits
        self.producto = etf("spyi", "SPYI.DE", **SPYI)

    def construye(self, cfg):
        with PreciosInventados({"SPYI.DE": SERIE_25}):
            return motor.construir(cfg, self.carpeta, descargar=False)


class DeduceElSplit(ConSplitSinEvento):
    def test_participaciones_valor_y_rentabilidad(self):
        D = self.construye(cartera([self.producto], compras_25()))
        p = D["productos"][0]
        self.assertAlmostEqual(p["participaciones"], 15 * 50)
        self.assertAlmostEqual(p["valor"], 750 * SERIE_25[p["navFecha"]], places=2)
        rent = D["rentabilidadAnual"]
        for ano, r in zip(rent["anos"], rent["porProducto"]["spyi"]):
            with self.subTest(ano=ano):
                self.assertGreater(r, -0.05)       # sube un poco cada día: nada de -96 %
                self.assertLess(r, 0.3)

    def test_avisa_de_que_lo_deduce(self):
        D = self.construye(cartera([self.producto], compras_25()))
        avisos = [a for a in D["avisos"] if "split" in a]
        self.assertEqual(avisos, ["Stt StrtSPDRMSCIAllCount: deduzco un split de 25 por 1 entre el 16/02/2026 y "
                                  "el 16/03/2026 por el precio de tus compras; cuento en participaciones de "
                                  "después del split 11 movimientos anteriores."])
        self.assertFalse(any("no cuadra" in a for a in D["avisos"]), D["avisos"])

    def test_corregidas_a_mano_dan_lo_mismo_sin_aviso(self):
        D1 = self.construye(cartera([self.producto], compras_25()))
        D2 = self.construye(cartera([self.producto], compras_25(corregidas=True)))
        p1, p2 = D1["productos"][0], D2["productos"][0]
        for k in ("participaciones", "valor", "aportado", "plusvalia"):
            with self.subTest(campo=k):
                self.assertAlmostEqual(p1[k], p2[k], places=6)
        self.assertEqual(D1["rentabilidadAnual"]["porProducto"], D2["rentabilidadAnual"]["porProducto"])
        self.assertFalse(any("split" in a or "no cuadra" in a for a in D2["avisos"]), D2["avisos"])

    def test_ajusta_cada_movimiento_y_no_toca_los_originales(self):
        movs = compras_25()
        nuevos, resumen = motor.ajusta_splits(movs, [self.producto], cache=self.cache)
        self.assertEqual([round(m["unidades"], 9) for m in nuevos], [50] * 15)
        self.assertEqual([m["unidades"] for m in movs], [2] * 11 + [50] * 4)
        self.assertTrue(all(n is m for n, m in zip(nuevos[11:], movs[11:])))   # los que no cambian, los mismos
        self.assertEqual(resumen["spyi"]["movimientos"], 11)
        self.assertEqual(resumen["spyi"]["splits"], {"2026-03-16": 25.0})

    def test_un_ajuste_anterior_al_split_se_multiplica(self):
        movs = compras_25()
        movs.insert(3, mov("2025-07-02", "spyi", "ajuste", 10, 0, mid="aj"))
        nuevos, _ = motor.ajusta_splits(movs, [self.producto], cache=self.cache)
        self.assertAlmostEqual(nuevos[3]["unidades"], 250)
        nuevos, _ = motor.ajusta_splits(movs + [mov("2026-04-01", "spyi", "ajuste", 600, 0)], [self.producto],
                                        cache=self.cache)
        self.assertAlmostEqual(nuevos[-1]["unidades"], 600)          # después del split, tal cual

    def test_unidades_y_lotes_de_un_dia_anterior_al_split(self):
        cfg = cartera([self.producto], compras_25())
        self.assertAlmostEqual(almacen.unidades_a(cfg, "spyi", "2025-10-01", cache=self.cache), 6 * 2)
        self.assertAlmostEqual(almacen.unidades_a(cfg, "spyi", "2026-04-01", cache=self.cache), 12 * 50)
        lotes = motor.lotes_vivos(cfg, "spyi", "2025-10-01", cache=self.cache)
        self.assertEqual([round(l[0], 6) for l in lotes], [2] * 6)
        lotes = motor.lotes_vivos(cfg, "spyi", "2026-04-01", cache=self.cache)
        self.assertEqual([round(l[0], 6) for l in lotes], [50] * 12)

    def test_errata_no_se_ajusta_y_avisa(self):
        movs = compras_25()
        f = "2026-05-04"
        # 6,5 veces el cierre: lejos de 5 y de 8 (con 7,3 ya estaría a menos de un 10 % de 8).
        movs.append(mov(f, "spyi", "compra", 10, round(10 * 6.5 * cierre(SERIE_25, f), 2), mid="raro"))
        nuevos, _ = motor.ajusta_splits(movs, [self.producto], cache=self.cache)
        self.assertEqual(nuevos[-1]["unidades"], 10)
        D = self.construye(cartera([self.producto], movs))
        raros = [a for a in D["avisos"] if "no cuadra" in a]
        self.assertEqual(len(raros), 1, D["avisos"])
        self.assertIn("Stt StrtSPDRMSCIAllCount", raros[0])
        self.assertIn("04/05/2026", raros[0])
        self.assertAlmostEqual(D["productos"][0]["participaciones"], 760)

    def test_producto_sin_movimientos_o_sin_cierres(self):
        nuevos, resumen = motor.ajusta_splits([], [self.producto], cache=self.cache)
        self.assertEqual((nuevos, resumen), ([], {}))
        movs = compras_25(pid="x")
        nuevos, resumen = motor.ajusta_splits(movs, [etf("x", "SINCIERRES.DE")], cache=self.cache)
        self.assertEqual(nuevos, movs)
        self.assertEqual(resumen, {})


class DeduceEnOtraMoneda(ConCarpeta):
    """Un producto en dólares: el cierre se pasa a euros con el cambio guardado de cada día."""

    SERIE_USD = serie_inventada(base=30, desde="2024-01-01", paso=0.0003)
    CAMBIO = serie_inventada(base=0.9, desde="2024-01-01", paso=0.0001)
    ANTES, DESPUES = ["2024-03-01", "2024-06-03"], ["2025-01-02", "2025-06-02"]

    def setUp(self):
        super().setUp()
        self.cache = guarda_cache_yahoo(self.carpeta, "USA", self.SERIE_USD)
        guarda_cache_yahoo(self.carpeta, "USDEUR=X", self.CAMBIO)
        self.producto = etf("us", "USA", moneda="USD")

    def compra(self, f, unidades, split=1):
        return mov(f, "us", "compra", unidades,
                   round(unidades * split * cierre(self.SERIE_USD, f) * cierre(self.CAMBIO, f), 2))

    def test_split_de_4_por_1(self):
        movs = [self.compra(f, 3, split=4) for f in self.ANTES] + [self.compra(f, 12) for f in self.DESPUES]
        nuevos, resumen = motor.ajusta_splits(movs, [self.producto], cache=self.cache)
        self.assertEqual([round(m["unidades"], 9) for m in nuevos], [12] * 4)
        self.assertEqual(resumen["us"]["splits"], {"2025-01-02": 4.0})

    def test_el_cambio_solo_no_es_un_split(self):
        movs = [self.compra(f, 12) for f in self.ANTES + self.DESPUES]
        nuevos, resumen = motor.ajusta_splits(movs, [self.producto], cache=self.cache)
        self.assertEqual(nuevos, movs)
        self.assertEqual(resumen, {})
        # Sin el cambio guardado se compara en la moneda del producto: 0,9 tampoco es un split.
        nuevos, resumen = motor.ajusta_splits(movs, [etf("us", "USA", moneda="CHF")], cache=self.cache)
        self.assertEqual(nuevos, movs)
        self.assertEqual(resumen, {})


if __name__ == "__main__":
    unittest.main()
