"""Pruebas funcionales de cada módulo del panel de gestión (casos válidos y de error)."""
import re, datetime
import pytest
from playwright.sync_api import expect
from conftest import BASE, SUFIJO, rut_con_dv
from ayudas import ir, sin_errores_js, clic_y_confirmar, abrir_modal

NOMBRE_CLI = f"PRUEBA{SUFIJO}"
RUT_CLI = rut_con_dv(15000000 + int(SUFIJO[-6:]))
MODAL_ENVIAR = ".modal-overlay:visible button[type=submit], .modal:visible button[type=submit]"


def existe(page, texto, espera=10000):
    """True si el texto aparece en la página (sin distinguir mayúsculas)."""
    try:
        expect(page.locator("body")).to_contain_text(re.compile(re.escape(texto), re.I), timeout=espera)
        return True
    except AssertionError:
        return False


def enviar_modal(page):
    """Envía el formulario del modal; si el sistema pide confirmar, confirma."""
    boton = page.locator(MODAL_ENVIAR).first
    texto = boton.inner_text().strip()
    boton.click()
    page.wait_for_timeout(900)
    if texto:
        iguales = page.get_by_role("button", name=texto, exact=True)
        if iguales.count() > 1 and iguales.last.is_visible():
            iguales.last.click()
    page.wait_for_timeout(2500)


# ---------------------------------------------------------------- inventario
def test_G10_inventario_busqueda_filtro_y_detalle(admin, evidencia):
    ir(admin, "/gestion/inventario/")
    filas = admin.locator("a[href^='/gestion/inventario/']")
    assert filas.count() > 5
    evidencia(admin, "listado")
    admin.get_by_role("button", name="Solo stock bajo").click()
    admin.wait_for_timeout(1200)
    evidencia(admin, "stock_bajo")
    admin.get_by_role("button", name="Limpiar").click()
    admin.wait_for_timeout(800)
    admin.get_by_role("button", name=re.compile("Categorías")).click()
    admin.wait_for_timeout(800)
    evidencia(admin, "categorias")
    ir(admin, "/gestion/inventario/61/")
    expect(admin.locator("body")).to_contain_text("Copa de vino blanco")
    expect(admin.locator("body")).to_contain_text(re.compile("stock total", re.I))
    evidencia(admin, "detalle_producto")
    sin_errores_js(admin)


def test_G11_inventario_crear_categoria_vacia_y_valida(admin, evidencia):
    ir(admin, "/gestion/inventario/")
    abrir_modal(admin, "+ Nueva categoría")
    evidencia(admin, "modal_categoria")
    enviar_modal(admin)  # vacío: debe seguir pidiendo el nombre
    expect(admin).to_have_url(re.compile("/gestion/inventario"))
    assert f"Categoría {NOMBRE_CLI}" not in admin.inner_text("body")
    evidencia(admin, "categoria_vacia_rechazada")
    ir(admin, "/gestion/inventario/")
    abrir_modal(admin, "+ Nueva categoría")
    admin.locator("input[name=nombre]:visible").first.fill(f"Categoría {NOMBRE_CLI}")
    admin.locator("input[name=descripcion]:visible").first.fill("Creada por la prueba automática")
    evidencia(admin, "categoria_completa")
    enviar_modal(admin)
    ir(admin, "/gestion/inventario/")
    abrir_modal(admin, re.compile("Categorías"))
    expect(admin.locator("body")).to_contain_text(f"Categoría {NOMBRE_CLI}")
    evidencia(admin, "categoria_creada")


def test_G12_inventario_crear_y_editar_producto(admin, evidencia):
    ir(admin, "/gestion/inventario/")
    abrir_modal(admin, "+ Nuevo producto")
    evidencia(admin, "modal_producto")
    enviar_modal(admin)  # vacío
    assert f"PRUEBA producto {SUFIJO}" not in admin.inner_text("body")
    evidencia(admin, "producto_vacio_rechazado")
    ir(admin, "/gestion/inventario/")
    abrir_modal(admin, "+ Nuevo producto")
    admin.locator("input[name=nombre]:visible").first.fill(f"PRUEBA producto {SUFIJO}")
    admin.locator("input[name=codigo]:visible").first.fill(f"PRB-{SUFIJO[-6:]}")
    admin.locator("select[name=categoria_id]:visible").first.select_option(index=1)
    admin.locator("input[name=precio_arriendo]:visible").first.fill("700")
    admin.locator("input[name=costo_reposicion]:visible").first.fill("2500")
    admin.locator("input[name=stock_total]:visible").first.fill("50")
    admin.locator("input[name=stock_trabajable]:visible").first.fill("50")
    evidencia(admin, "producto_completo")
    enviar_modal(admin)
    ir(admin, f"/gestion/inventario/?q=PRB-{SUFIJO[-6:]}")
    expect(admin.locator("body")).to_contain_text(f"PRUEBA producto {SUFIJO}")
    evidencia(admin, "producto_en_listado")
    # editar: subir stock y precio
    href = admin.locator("a", has_text=f"PRUEBA producto {SUFIJO}").first.get_attribute("href")
    ir(admin, href)
    admin.get_by_role("button", name=re.compile("Editar producto")).click()
    admin.wait_for_timeout(700)
    admin.locator("input[name=precio_arriendo]:visible").first.fill("750")
    admin.locator("input[name=motivo]:visible").first.fill("Ajuste de prueba automática")
    evidencia(admin, "editar_producto")
    enviar_modal(admin)
    expect(admin.locator("body")).to_contain_text("750")
    evidencia(admin, "producto_editado")


def test_G13_producto_activo_aparece_en_catalogo_publico(admin, evidencia):
    ir(admin, f"/gestion/inventario/?q=PRB-{SUFIJO[-6:]}")
    if not existe(admin, f"PRUEBA producto {SUFIJO}"):
        pytest.skip("Depende de G12")
    admin.goto(BASE + "/catalogo/")
    expect(admin.locator("body")).to_contain_text(f"PRUEBA producto {SUFIJO}")
    evidencia(admin, "producto_visible_en_catalogo")


# ---------------------------------------------------------------- clientes
def test_G14_clientes_validaciones_y_alta(admin, evidencia):
    ir(admin, "/gestion/clientes/")
    abrir_modal(admin, "+ Nuevo cliente")
    evidencia(admin, "modal_cliente")
    enviar_modal(admin)  # vacío
    assert NOMBRE_CLI not in admin.inner_text("body")
    # RUT inválido
    ir(admin, "/gestion/clientes/")
    abrir_modal(admin, "+ Nuevo cliente")
    admin.fill("input[name=nombre]:visible", NOMBRE_CLI)
    admin.fill("input[name=apellido]:visible", "Gestión")
    admin.fill("input[name=rut]:visible", "11.111.111-1")
    admin.fill("input[name=telefono]:visible", "+56 9 5555 5555")
    admin.fill("input[name=email]:visible", f"cli.{SUFIJO}@example.com")
    enviar_modal(admin)
    evidencia(admin, "rut_invalido_rechazado")
    ir(admin, f"/gestion/clientes/?q={NOMBRE_CLI}")
    assert NOMBRE_CLI not in admin.inner_text("table") if admin.locator("table").count() else True
    # válido
    ir(admin, "/gestion/clientes/")
    abrir_modal(admin, "+ Nuevo cliente")
    admin.fill("input[name=nombre]:visible", NOMBRE_CLI)
    admin.fill("input[name=apellido]:visible", "Gestión")
    admin.fill("input[name=rut]:visible", RUT_CLI)
    admin.fill("input[name=telefono]:visible", "+56 9 5555 5555")
    admin.fill("input[name=email]:visible", f"cli.{SUFIJO}@example.com")
    admin.fill("input[name=comuna]:visible", "Maipú")
    admin.fill("input[name=direccion]:visible", "Calle Prueba 456")
    evidencia(admin, "cliente_completo")
    enviar_modal(admin)
    ir(admin, f"/gestion/clientes/?q={NOMBRE_CLI}")
    expect(admin.locator("body")).to_contain_text(NOMBRE_CLI)
    evidencia(admin, "cliente_en_listado")


def test_G15_cliente_duplicado_por_rut_es_rechazado(admin, evidencia):
    ir(admin, f"/gestion/clientes/?q={NOMBRE_CLI}")
    if not existe(admin, NOMBRE_CLI):
        pytest.skip("Depende de G14")
    ir(admin, "/gestion/clientes/")
    abrir_modal(admin, "+ Nuevo cliente")
    admin.fill("input[name=nombre]:visible", f"Duplicado{SUFIJO}")
    admin.fill("input[name=apellido]:visible", "Gestión")
    admin.fill("input[name=rut]:visible", RUT_CLI)
    admin.fill("input[name=telefono]:visible", "+56 9 5555 5555")
    admin.fill("input[name=email]:visible", f"dup.{SUFIJO}@example.com")
    enviar_modal(admin)
    ir(admin, f"/gestion/clientes/?q=Duplicado{SUFIJO}")
    assert not existe(admin, f"Duplicado{SUFIJO}", espera=3000)
    evidencia(admin, "duplicado_rechazado")


def test_G16_cliente_ficha_y_edicion(admin, evidencia):
    ir(admin, f"/gestion/clientes/?q={NOMBRE_CLI}")
    if not existe(admin, NOMBRE_CLI):
        pytest.skip("Depende de G14")
    href = admin.locator("a[href^='/gestion/clientes/']", has_text=re.compile(NOMBRE_CLI, re.I)).first.get_attribute("href")
    ir(admin, href)
    expect(admin.locator("body")).to_contain_text("Servicios del cliente")
    evidencia(admin, "ficha_cliente")
    admin.get_by_role("button", name="Editar").first.click()
    admin.wait_for_timeout(700)
    admin.fill("input[name=comuna]:visible", "Providencia")
    evidencia(admin, "editar_cliente")
    enviar_modal(admin)
    expect(admin.locator("body")).to_contain_text("Providencia")
    evidencia(admin, "cliente_editado")


# ---------------------------------------------------------------- cotizaciones / reservas
def test_G17_cotizaciones_filtros(admin, evidencia):
    ir(admin, "/gestion/cotizaciones/")
    filas = admin.locator("a[href^='/gestion/cotizaciones/']")
    assert filas.count() >= 1
    evidencia(admin, "listado")
    admin.locator("select[name=estado]").select_option(label="Cerrado")
    admin.get_by_role("button", name="Buscar").click()
    admin.wait_for_timeout(1500)
    evidencia(admin, "filtro_cerrado")
    expect(admin.locator("body")).to_contain_text("Cerrado")
    ir(admin, "/gestion/cotizaciones/?q=zzzzzz-no-existe")
    evidencia(admin, "busqueda_sin_resultados")


def test_G18_nueva_cotizacion_vacia_y_valida(admin, evidencia):
    ir(admin, "/gestion/cotizaciones/")
    abrir_modal(admin, "+ Nueva cotización")
    evidencia(admin, "modal_cotizacion")
    enviar_modal(admin)  # vacío
    expect(admin).to_have_url(re.compile("/gestion/cotizaciones/?(\\?.*)?$"))
    ir(admin, "/gestion/cotizaciones/")
    abrir_modal(admin, "+ Nueva cotización")
    sel = admin.locator("select[name=cliente_id]:visible")
    opciones = sel.locator("option").all_inner_texts()
    idx = next((i for i, t in enumerate(opciones) if NOMBRE_CLI in t), 1)
    sel.select_option(index=idx)
    admin.fill("input[name=tipo_servicio]:visible", "Cumpleaños")
    fecha = (datetime.date.today() + datetime.timedelta(days=20)).isoformat()
    for campo in ("fecha_evento", "fecha_inicio", "fecha_termino"):
        admin.fill(f"input[name={campo}]:visible", fecha)
    admin.fill("input[name=comuna_evento]:visible", "Maipú")
    admin.fill("input[name=direccion_evento]:visible", "Calle Prueba 456")
    admin.fill("input[name=cantidad_personas]:visible", "40")
    evidencia(admin, "cotizacion_completa")
    enviar_modal(admin)
    expect(admin).to_have_url(re.compile(r"/gestion/cotizaciones/\d+/"))
    evidencia(admin, "cotizacion_creada")
    sin_errores_js(admin)


# ---------------------------------------------------------------- listados operativos
def test_G19_reservas_listado_y_detalle(admin, evidencia):
    ir(admin, "/gestion/reservas/")
    expect(admin.locator("body")).to_contain_text("Reservas")
    evidencia(admin, "listado_reservas")
    ir(admin, "/gestion/reservas/20/")
    expect(admin.locator("body")).to_contain_text(re.compile("trazabilidad", re.I))
    expect(admin.locator("body")).to_contain_text("Cerrado")
    evidencia(admin, "detalle_reserva_cerrada")


def test_G20_preparacion_listado_filtros_y_hoja(admin, evidencia):
    ir(admin, "/gestion/preparacion/")
    evidencia(admin, "listado")
    for estado in ("Completa", "Pendiente", "En%20preparacion"):
        ir(admin, f"/gestion/preparacion/?estado={estado}")
    admin.goto(BASE + "/gestion/preparacion/1/")
    admin.add_init_script("window.print = function(){ window.__impreso = true }")
    expect(admin.locator("body")).to_contain_text("Artículos a preparar")
    evidencia(admin, "hoja_preparacion")


def test_G21_despachos_y_devoluciones_listados_con_filtros(admin, evidencia):
    for vista in ("", "?vista=hoy", "?vista=pendientes", "?vista=despachados", "?vista=semana"):
        ir(admin, f"/gestion/despachos/{vista}")
    evidencia(admin, "despachos")
    ir(admin, "/gestion/despachos/1/")
    expect(admin.locator("body")).to_contain_text(re.compile("despacho", re.I))
    evidencia(admin, "detalle_despacho")
    for estado in ("", "?estado=pendientes", "?estado=parcial", "?estado=devueltos", "?estado=vencidos"):
        ir(admin, f"/gestion/devoluciones/{estado}")
    evidencia(admin, "devoluciones")
    ir(admin, "/gestion/devoluciones/reserva/20/")
    expect(admin.locator("body")).to_contain_text(re.compile("merma", re.I))
    evidencia(admin, "detalle_devolucion")


# ---------------------------------------------------------------- mermas, calendario, reportes
def test_G22_mermas_validacion_y_registro(admin, evidencia):
    ir(admin, "/gestion/mermas/")
    evidencia(admin, "listado_mermas")
    abrir_modal(admin, "+ Registrar merma")
    enviar_modal(admin)  # vacío
    expect(admin).to_have_url(re.compile("/gestion/mermas"))
    evidencia(admin, "merma_vacia")
    ir(admin, "/gestion/mermas/")
    abrir_modal(admin, "+ Registrar merma")
    sel_art = admin.locator("select[name=articulo_id]:visible")
    etiqueta = next(t for t in sel_art.locator("option").all_inner_texts() if "Vaso old fashioned" in t)
    sel_art.select_option(label=etiqueta)
    tipo = admin.locator("select[name=tipo]:visible").first
    tipo.select_option(index=1)
    admin.fill("input[name=cantidad]:visible", "1")
    admin.fill("input[name=motivo]:visible", f"PRUEBA motivo {SUFIJO}")
    evidencia(admin, "merma_completa")
    enviar_modal(admin)
    ir(admin, "/gestion/mermas/")
    expect(admin.locator("body")).to_contain_text(f"PRUEBA motivo {SUFIJO}")
    evidencia(admin, "merma_registrada")


def test_G23_calendario_vistas(admin, evidencia):
    ir(admin, "/gestion/calendario/")
    evidencia(admin, "mes")
    for vista in ("Semana", "Día", "Mes"):
        admin.get_by_role("button", name=vista, exact=True).click()
        admin.wait_for_timeout(900)
    admin.locator("#cal-next").click()
    admin.wait_for_timeout(700)
    admin.locator("#cal-prev").click()
    admin.locator("#cal-hoy").click()
    evidencia(admin, "navegacion")
    sin_errores_js(admin)


def test_G24_reportes_rangos_de_fecha(admin, evidencia):
    ir(admin, "/gestion/reportes/")
    evidencia(admin, "reporte_inicial")
    hoy = datetime.date.today()
    admin.fill("input[name=desde]", (hoy - datetime.timedelta(days=90)).isoformat())
    admin.fill("input[name=hasta]", hoy.isoformat())
    admin.get_by_role("button", name="Aplicar").click()
    admin.wait_for_timeout(1500)
    evidencia(admin, "reporte_90_dias")
    # rango invertido no debe romper la página
    admin.fill("input[name=desde]", hoy.isoformat())
    admin.fill("input[name=hasta]", (hoy - datetime.timedelta(days=10)).isoformat())
    admin.get_by_role("button", name="Aplicar").click()
    admin.wait_for_timeout(1500)
    expect(admin.locator("body")).not_to_contain_text("Server Error")
    evidencia(admin, "rango_invertido")


# ---------------------------------------------------------------- usuarios y configuración
def test_G25_usuarios_validacion_y_alta(admin, evidencia):
    ir(admin, "/gestion/usuarios/")
    evidencia(admin, "listado")
    abrir_modal(admin, "+ Nuevo usuario")
    enviar_modal(admin)  # vacío
    expect(admin).to_have_url(re.compile("/gestion/usuarios"))
    ir(admin, "/gestion/usuarios/")
    abrir_modal(admin, "+ Nuevo usuario")
    admin.fill("input[name=nombre]:visible", "PRUEBA")
    admin.fill("input[name=apellido]:visible", f"Usuario{SUFIJO}")
    admin.fill("input[name=email]:visible", f"usuario.{SUFIJO}@example.com")
    admin.locator("select[name=rol_id]:visible").select_option(index=0)
    admin.fill("input[name=password]:visible", "Prueba2026!seguro")
    evidencia(admin, "usuario_completo")
    enviar_modal(admin)
    ir(admin, "/gestion/usuarios/")
    expect(admin.locator("body")).to_contain_text(f"usuario.{SUFIJO}@example.com")
    evidencia(admin, "usuario_creado")


def test_G26_configuracion_validacion_sin_guardar_cambios(admin, evidencia):
    """Se comprueba la validación vaciando un dato obligatorio; no se guardan cambios reales."""
    ir(admin, "/gestion/configuracion/")
    evidencia(admin, "configuracion")
    original = admin.input_value("input[name=nombre]")
    admin.fill("input[name=nombre]", "")
    admin.get_by_role("button", name="Guardar cambios").first.click()
    admin.wait_for_timeout(2000)
    ir(admin, "/gestion/configuracion/")
    assert admin.input_value("input[name=nombre]") == original
    evidencia(admin, "valor_original_intacto")
