"""Flujo operativo completo, de punta a punta, en una sola grabación:
cliente -> cotización -> productos -> envío -> reserva -> preparación -> despacho
-> devolución con merma -> cierre -> verificación en mermas, calendario y reportes.
"""
import re, datetime
from playwright.sync_api import expect
from conftest import BASE, SUFIJO, rut_con_dv
from ayudas import ir, sin_errores_js, clic_y_confirmar, abrir_modal

NOMBRE = f"PRUEBAFLUJO{SUFIJO}"


def enviar_modal(page):
    boton = page.locator(".modal-overlay:visible button[type=submit]").first
    texto = boton.inner_text().strip()
    boton.click()
    page.wait_for_timeout(900)
    iguales = page.get_by_role("button", name=texto, exact=True)
    if texto and iguales.count() > 1 and iguales.last.is_visible():
        iguales.last.click()
    page.wait_for_timeout(2500)


def test_G30_flujo_completo_cotizacion_a_cierre(admin, evidencia):
    p = admin
    hoy = datetime.date.today()
    evento = (hoy + datetime.timedelta(days=2)).isoformat()

    # 1. Cliente nuevo
    ir(p, "/gestion/clientes/")
    abrir_modal(p, "+ Nuevo cliente")
    p.fill("input[name=nombre]:visible", NOMBRE)
    p.fill("input[name=apellido]:visible", "Flujo")
    p.fill("input[name=rut]:visible", rut_con_dv(16000000 + int(SUFIJO[-6:])))
    p.fill("input[name=telefono]:visible", "+56 9 4444 4444")
    p.fill("input[name=email]:visible", f"flujo.{SUFIJO}@example.com")
    p.fill("input[name=comuna]:visible", "Maipú")
    p.fill("input[name=direccion]:visible", "Av. Prueba 789")
    enviar_modal(p)
    ir(p, f"/gestion/clientes/?q={NOMBRE}")
    expect(p.locator("body")).to_contain_text(re.compile(NOMBRE, re.I))
    evidencia(p, "01_cliente_creado")

    # 2. Nueva cotización
    ir(p, "/gestion/cotizaciones/")
    abrir_modal(p, "+ Nueva cotización")
    sel = p.locator("select[name=cliente_id]:visible")
    etiqueta = next(t for t in sel.locator("option").all_inner_texts() if NOMBRE.lower() in t.lower())
    sel.select_option(label=etiqueta)
    p.fill("input[name=tipo_servicio]:visible", "Cumpleaños")
    for campo in ("fecha_evento", "fecha_inicio", "fecha_termino"):
        p.fill(f"input[name={campo}]:visible", evento)
    p.fill("input[name=comuna_evento]:visible", "Maipú")
    p.fill("input[name=direccion_evento]:visible", "Av. Prueba 789")
    p.fill("input[name=cantidad_personas]:visible", "40")
    enviar_modal(p)
    expect(p).to_have_url(re.compile(r"/gestion/cotizaciones/\d+/"))
    evidencia(p, "02_cotizacion_creada")

    # 3. Agregar productos
    p.get_by_role("button", name="+ Agregar productos").click()
    p.wait_for_timeout(800)
    p.locator("tbody[data-grupo] [data-invitados]:visible").first.click()
    p.locator("[data-tab]").nth(1).click()
    p.wait_for_timeout(500)
    p.locator("tbody[data-grupo] [data-invitados]:visible").first.click()
    evidencia(p, "03_productos_elegidos")
    p.locator("#modal-agregar-articulo button[type=submit]").click()
    p.wait_for_timeout(3000)
    expect(p.locator("body")).to_contain_text(re.compile("DISPONIBLE", re.I))
    evidencia(p, "04_cotizacion_con_productos")

    # 4. Enviar la cotización al cliente
    p.get_by_text("Otro medio").click()
    p.wait_for_timeout(500)
    clic_y_confirmar(p, re.compile("La informé por teléfono"))
    expect(p.locator("body")).to_contain_text(re.compile("enviada por Teléfono", re.I))
    evidencia(p, "05_cotizacion_enviada")

    # 5. Confirmar reserva
    clic_y_confirmar(p, "Confirmar reserva")
    expect(p.locator("body")).to_contain_text(re.compile("Reserva confirmada", re.I))
    evidencia(p, "06_reserva_confirmada")
    p.get_by_role("link", name=re.compile("Ir a la reserva")).click()
    p.wait_for_url(re.compile(r"/gestion/reservas/\d+/"))
    servicio_id = re.search(r"/reservas/(\d+)/", p.url).group(1)
    evidencia(p, "07_detalle_reserva")

    # 6. Preparación
    clic_y_confirmar(p, "Generar preparación")
    expect(p).to_have_url(re.compile(r"/gestion/preparacion/\d+/"))
    p.add_init_script("window.print = function(){ window.__impreso = true }")
    p.evaluate("window.print = function(){ window.__impreso = true }")
    evidencia(p, "08_hoja_de_preparacion")
    p.get_by_role("button", name=re.compile("Imprimir hojas por categoría")).click()
    p.wait_for_timeout(1200)
    p.locator("textarea[name=observaciones]").fill("PRUEBA automática: pedido revisado.")
    p.get_by_role("button", name="Guardar observaciones").click()
    p.wait_for_timeout(2500)
    clic_y_confirmar(p, re.compile("Marcar pedido como preparado"))
    expect(p.locator("body")).to_contain_text(re.compile("Registrar despacho", re.I))
    evidencia(p, "09_pedido_preparado")

    # 7. Despacho
    ir(p, f"/gestion/despachos/registrar/{servicio_id}/")
    p.fill("input[name=fecha_salida]", datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"))
    p.fill("input[name=fecha_devolucion]", p.get_attribute("input[name=fecha_devolucion]", "min"))
    p.fill("input[name=responsable]", "PRUEBA Responsable")
    p.fill("input[name=receptor]", "PRUEBA Receptor")
    evidencia(p, "10_formulario_despacho")
    clic_y_confirmar(p, "Confirmar despacho")
    expect(p.locator("body")).to_contain_text(re.compile("Despacho registrado", re.I))
    evidencia(p, "11_despacho_registrado")

    # 8. Devolución con merma
    ir(p, f"/gestion/devoluciones/registrar/{servicio_id}/")
    filas = p.locator("tr:has(input[name=buenas])")
    assert filas.count() == 2
    for i in range(filas.count()):
        fila = filas.nth(i)
        if "Vaso" in fila.inner_text():
            fila.locator("input[name=buenas]").fill("37")
            fila.locator("input[name=merma]").fill("3")
            fila.locator("input[name=observacion]").fill("PRUEBA: 3 vasos quebrados")
        else:
            fila.get_by_role("button", name="Todo").click()
    p.fill("input[name=entregado_por]", "PRUEBA Cliente")
    evidencia(p, "12_formulario_devolucion")
    clic_y_confirmar(p, "Registrar devolución")
    expect(p.locator("body")).to_contain_text(re.compile("Devolución registrada", re.I))
    evidencia(p, "13_devolucion_registrada")

    # 9. Cierre
    clic_y_confirmar(p, "Cerrar reserva")
    ir(p, f"/gestion/reservas/{servicio_id}/")
    expect(p.locator("body")).to_contain_text(re.compile("Cerrado", re.I))
    evidencia(p, "14_reserva_cerrada")

    # 10. Verificaciones cruzadas
    ir(p, "/gestion/mermas/")
    expect(p.locator("body")).to_contain_text(re.compile("3 vasos quebrados", re.I))
    evidencia(p, "15_merma_en_listado")
    ir(p, "/gestion/calendario/")
    evidencia(p, "16_calendario")
    ir(p, "/gestion/reportes/")
    evidencia(p, "17_reportes")
    ir(p, "/gestion/")
    evidencia(p, "18_resumen_final")
    sin_errores_js(p)
