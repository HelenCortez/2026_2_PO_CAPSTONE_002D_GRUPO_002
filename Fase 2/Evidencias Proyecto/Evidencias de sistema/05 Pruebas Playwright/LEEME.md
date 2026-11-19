# Pruebas automáticas con video — Entre Platos y Copas

Sistema probado: https://epyc-in7j.onrender.com (Render). Herramienta: Playwright (Python) + pytest.
Cada prueba abre un navegador real, ejecuta los pasos a velocidad humana y deja:

- `videos/`      un video `.webm` por prueba (la evidencia que pidió el profesor), ordenados en 14 subcarpetas por tema (01_Login_y_acceso, 04_Cotizador_publico, 14_Flujo_completo, etc.). Si se vuelven a ejecutar las pruebas, los videos nuevos quedan sueltos en `videos/`.
- `evidencias/`  capturas de pantalla de los pasos importantes
- `informe/informe_pruebas.html`  informe con aprobado/fallido por prueba (abrir en el navegador)

## Cómo repetirlas
1. Dejar un archivo `.env.local` en esta carpeta con: `BASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` (no se comparte).
2. `pip install pytest-playwright pytest-html` y `python -m playwright install chromium`
3. `python -m pytest` (todas) o `python -m pytest tests/test_flujo_completo.py` (una sola).
   Más lento para grabar: `SLOW_MO=600`. Más rápido: `SLOW_MO=0`.

## Qué cubre
| Archivo | Pruebas | Contenido |
|---|---|---|
| test_publico.py | P01–P15 | inicio, catálogo y filtros, producto, 404, nosotros/contacto, cotizador (campos vacíos, invitados 0, categoría obligatoria, RUT inválido, solicitud válida), registro de cliente (vacío, claves distintas, válido), login incorrecto, celular |
| test_gestion_acceso.py | G01–G09 | rutas protegidas sin sesión, login incorrecto/vacío/correcto, mostrar clave, cerrar sesión, recorrido por los 12 módulos, 404 en ids inexistentes, rechazo de POST sin token CSRF, servicio de búsqueda, panel en celular |
| test_gestion_modulos.py | G10–G26 | inventario (filtros, categoría, producto crear/editar, aparece en catálogo público), clientes (validaciones, RUT inválido, duplicado, ficha, edición), cotizaciones (filtros, alta), reservas, preparación, despachos, devoluciones, mermas, calendario, reportes (incluye rango invertido), usuarios, configuración |
| test_flujo_completo.py | G30 | cliente → cotización → productos → envío → reserva → preparación → despacho → devolución con merma → cierre → verificación en mermas/calendario/reportes |
| test_reglas_negocio.py | G31–G34 | bloqueo por stock insuficiente, cancelación con motivo, despacho/devolución inexistentes o sin despacho previo |

## Datos de prueba
Las pruebas crean registros reales en el sistema con el prefijo **PRUEBA** (clientes, productos,
categorías, cotizaciones, usuarios, una merma de 3 unidades). Pueden borrarse o dejarse como evidencia.

## Observación detectada
La caja de búsqueda superior del panel está oculta por CSS (`.topbar-search { display: none }`
en `admin.css`); el servicio `/gestion/buscar/` sí funciona y se prueba directamente (G08).
