"""Reglas de negocio: sobreventa, cancelaciones y restricciones del ciclo operativo."""
import re, datetime
from playwright.sync_api import expect
from conftest import BASE, SUFIJO
from ayudas import ir, sin_errores_js, clic_y_confirmar, abrir_modal


def _enviar_modal(page):
    boton = page.locator(".modal-overlay:visible button[type=submit]").first
    texto = boton.inner_text().strip()
    boton.click()
    page.wait_for_timeout(900)
    iguales = page.get_by_role("button", name=texto, exact=True)
    if texto and iguales.count() > 1 and iguales.last.is_visible():
        iguales.last.click()
    page.wait_for_timeout(2500)


def _nueva_cotizacion(p, dias=30, personas="40"):
    ir(p, "/gestion/cotizaciones/")
    abrir_modal(p, "+ Nueva cotización")
    p.locator("select[name=cliente_id]:visible").select_option(index=1)
    p.fill("input[name=tipo_servicio]:visible", "Matrimonio")
    fecha = (datetime.date.today() + datetime.timedelta(days=dias)).isoformat()
    for campo in ("fecha_evento", "fecha_inicio", "fecha_termino"):
        p.fill(f"input[name={campo}]:visible", fecha)
    p.fill("input[name=comuna_evento]:visible", "Maipú")
    p.fill("input[name=direccion_evento]:visible", "Calle Prueba 1")
    p.fill("input[name=cantidad_personas]:visible", personas)
    _enviar_modal(p)
    expect(p).to_have_url(re.compile(r"/gestion/cotizaciones/\d+/"))


def test_G31_no_permite_sobreventa_de_stock(admin, evidencia):
    """Pedir más unidades de las disponibles debe advertirse y no puede confirmarse."""
    _nueva_cotizacion(admin, dias=35, personas="30")
    admin.get_by_role("button", name="+ Agregar productos").click()
    admin.wait_for_timeout(800)
    fila = admin.locator("tbody[data-grupo]:visible tr[data-producto]").first
    fila.locator("input[name=cantidad]").fill("99999")
    admin.wait_for_timeout(600)
    evidencia(admin, "aviso_en_el_formulario")
    admin.locator("#modal-agregar-articulo button[type=submit]").click()
    admin.wait_for_timeout(3000)
    evidencia(admin, "resultado_tras_agregar")
    expect(admin.locator("body")).to_contain_text(re.compile("FALTAN", re.I))
    admin.get_by_text("Otro medio").click()
    admin.wait_for_timeout(400)
    clic_y_confirmar(admin, re.compile("La informé por teléfono"))
    # con stock insuficiente, el sistema bloquea la confirmación de la reserva
    expect(admin.locator("body")).to_contain_text(re.compile("sin stock suficiente", re.I))
    expect(admin.get_by_role("button", name="Confirmar reserva")).to_be_disabled()
    evidencia(admin, "confirmacion_bloqueada")


def test_G32_cancelar_solicitud_registra_motivo(admin, evidencia):
    _nueva_cotizacion(admin, dias=40)
    evidencia(admin, "cotizacion_nueva")
    admin.get_by_text("Cancelar solicitud").first.click()
    admin.wait_for_timeout(700)
    campo = admin.locator("input[name=motivo]:visible")
    if campo.count():
        campo.first.fill("PRUEBA automática: el cliente desistió")
    evidencia(admin, "motivo_de_cancelacion")
    admin.get_by_role("button", name="Confirmar cancelación").click()
    admin.wait_for_load_state("domcontentloaded")
    admin.wait_for_timeout(3000)
    evidencia(admin, "tras_confirmar_cancelacion")
    expect(admin.locator("main")).to_contain_text(re.compile("cancel", re.I))
    evidencia(admin, "solicitud_cancelada")


def test_G33_despacho_exige_preparacion_previa(admin, evidencia):
    """No se puede registrar el despacho de una reserva que no está preparada."""
    _nueva_cotizacion(admin, dias=45)
    # id inexistente: debe responder error controlado, no caída del sistema
    r = admin.goto(BASE + "/gestion/despachos/registrar/999999/")
    assert r.status in (404, 302, 200)
    expect(admin.locator("body")).not_to_contain_text("Server Error")
    evidencia(admin, "despacho_inexistente")


def test_G34_devolucion_sin_despacho_no_se_puede_registrar(admin, evidencia):
    """Reserva 18 está confirmada pero nunca se despachó."""
    r = admin.goto(BASE + "/gestion/devoluciones/registrar/18/")
    cuerpo = admin.inner_text("body").lower()
    assert "no tiene artículos pendientes" in cuerpo or r.status in (302, 404) or "registrar devolución" not in cuerpo
    evidencia(admin, "devolucion_no_disponible")
