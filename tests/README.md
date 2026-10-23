# Pruebas de Rumbo

Comprueban que una versión nueva no rompe nada antes de publicarla. Funcionan **sin internet**
(los precios son inventados) y **nunca tocan `mis_datos`**: cada prueba usa una carpeta temporal.

## Cómo pasarlas

- **Windows**: doble clic en `tests\ejecutar_tests.bat`.
- **A mano**, desde la carpeta de la app:

  ```
  .venv\Scripts\python.exe -m unittest discover -s tests -b
  ```

Si todo va bien, termina con `OK`. Las cuentas del navegador y la imagen de «Compartir mi mes» se prueban con Node (`node --test "tests/js/*.test.js"`); si no tienes Node, esas se saltan. Si algo falla, sale qué prueba y por qué.

## Qué comprueban

| Archivo | Qué |
|---|---|
| `test_numeros.py` | Números y fechas como los escribe la gente (`1.000`, `10/03/2025`, fechas imposibles…). |
| `test_motor.py` | FIFO, TIR, divisas, valores a mano y la **cartera de ejemplo** (que no cambie nada). |
| `test_almacen.py` | Validaciones en castellano, colores, copias automáticas. |
| `test_importar.py` | CSV de MyInvestor (con traspasos), plantilla Excel/CSV, texto de la IA, duplicados. |
| `test_red.py` | Internet simulado: precios diarios y splits de Yahoo, reintentos, mensajes sin conexión. |
| `test_splits.py` | Splits: las participaciones de antes del split cuentan como las de después (valor, rentabilidad, ventas, traspasos, Renta y «Vender todo»), sin ajustar dos veces las que ya corregiste a mano. Si Yahoo no da el split, se deduce del precio de tus movimientos (también en otra moneda) y avisa de los que no cuadran con el de mercado. |
| `test_servidor.py` | La app de verdad con una carpeta temporal: demo, guardar, copias, web sin importes. |
| `test_fase_b.py` | Dividendos, exportar a Excel, compras en otra moneda, lanzador de Linux. |
| `test_repo.py` | Antes de publicar: misma versión en todas partes, lanzadores bien guardados y ningún dato personal. |
| `test_seguridad.py` | Otra web no puede leer ni cambiar tus datos; nombres con código; novedades; informe de soporte. |
| `test_traspasos.py` | Traspasos (total, parcial, antigüedad, cadenas, planes a mano), importar y cuadrar participaciones. |
| `test_perfiles.py` | Perfiles, «Todos juntos» (solo lectura) y que cada perfil tenga sus copias. |
| `test_exposicion.py` | Países y sectores con respuestas de Morningstar simuladas, y su cache. |
| `test_privacidad.py` | Llave de acceso, política de la página (CSP), copias manipuladas y **qué sale a internet** (ninguna cifra). |
| `test_docker.py` | Rumbo en Docker: abierta solo a direcciones de casa y siempre con la llave; los archivos de la imagen. |
| `test_myinvestor_real.py` | Extractos reales de MyInvestor con traspasos (importes escalados): detecta los lotes de traspaso y las participaciones dan el valor del extracto. |
| `test_traspasos_myinvestor.py` | Traspasos que llegan en el CSV de MyInvestor: «¿de qué fondo venía?», sin contar dos veces. |
| `test_calculos_js.py` | Las cuentas del navegador (`app/web/calculos.js`) y la imagen de «Compartir mi mes» (`compartir.js`) con Node, y que dan lo mismo que el motor. |
| `test_arreglos_v2.py` | Objetivo editable, vender todo, intereses de cuentas, pérdidas vendidas y reparto objetivo. |
| `test_muestra.py` | Muestra anónima de un extracto de otro banco: misma forma y nada tuyo (importes, fechas, IBAN, DNI, titular). |
| `test_ganancia_total.py` | «Ganancia total» de cada producto: precio + ventas (FIFO) + dividendos − comisiones, sin contar dos veces un traspaso. |
| `test_fiscal.py` | «Para la Renta»: ventas con su coste y fechas de compra, traspasos, regla de los dos meses, planes aparte, CSV. |
| `test_p2p.py` | Préstamos P2P y crowdfunding: tipo propio, importador, intereses y fuera de la bolsa. |
| `test_alquiler.py` | Pisos alquilados: alquiler y gastos de 12 meses, ganancia total y su sitio en la Renta. |
| `test_periodicas.py` | Aportaciones periódicas: fechas, precio de cada día, las ya anotadas se saltan y nada se guarda sin confirmar. |
| `test_acceso_directo.py` | El icono del Escritorio (Windows) y la entrada del menú de aplicaciones (Linux). |

## Cuando cambias algo a propósito

`datos/demo_esperado.json` es la "foto" de la cartera de ejemplo (participaciones, aportado y
realizado de cada producto). Si cambias la cartera de ejemplo o la forma de calcular **a propósito**,
vuelve a sacarla y revisa con `git diff` que los cambios son los que esperabas:

```
.venv\Scripts\python.exe tests\regenerar_esperados.py
```

Cada vez que se arregle un fallo, conviene añadir una prueba que lo habría detectado.
