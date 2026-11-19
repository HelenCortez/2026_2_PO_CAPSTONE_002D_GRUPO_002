"""Pruebas del sitio público (cliente sin sesión)."""
import re, datetime
import pytest
from playwright.sync_api import expect
from conftest import BASE, SUFIJO, rut_con_dv

RUT_PRUEBA = rut_con_dv(12000000 + int(SUFIJO[-6:]))
CORREO_PRUEBA = f"prueba.{SUFIJO}@example.com"
CLAVE_PRUEBA = "Prueba2026!seguro"
FECHA_FUTURA = (datetime.date.today() + datetime.timedelta(days=45)).isoformat()


def sin_errores_js(page):
    errores = [str(e) for e in page.consola if "favicon" not in str(e) and "wa.me" not in str(e)]
    assert not errores, f"Errores de consola/JS: {errores[:3]}"


# ---------------------------------------------------------------- navegación
def test_P01_inicio_carga_y_menu(pagina, evidencia):
    pagina.goto(BASE + "/")
    expect(pagina).to_have_title(re.compile("Entre Platos y Copas"))
    expect(pagina.get_by_role("link", name=re.compile("cotiz", re.I)).first).to_be_visible()
    evidencia(pagina, "inicio")
    for texto, url in [("Catálogo", "/catalogo/"), ("Nosotros", "/nosotros/"), ("Contacto", "/contacto/")]:
        pagina.get_by_role("link", name=texto).first.click()
        expect(pagina).to_have_url(re.compile(url))
        pagina.go_back()
    sin_errores_js(pagina)


def test_P02_catalogo_filtros_y_producto(pagina, evidencia):
    pagina.goto(BASE + "/catalogo/")
    expect(pagina).to_have_title(re.compile("Cat"))
    tarjetas = pagina.locator("a[href^='/producto/']")
    total = tarjetas.count()
    assert total > 5, "El catálogo debe mostrar productos"
    evidencia(pagina, "catalogo_todos")
    for cat in ["vajilla", "cristaleria", "cubiertos"]:
        pagina.get_by_role("button", name=cat, exact=True).click()
        pagina.wait_for_timeout(600)
        visibles = pagina.locator("a[href^='/producto/']:visible").count()
        assert 0 < visibles <= total
    evidencia(pagina, "catalogo_filtrado")
    pagina.get_by_role("button", name="Todas").click()
    pagina.locator("a[href^='/producto/']:visible").first.click()
    expect(pagina).to_have_url(re.compile(r"/producto/\d+/"))
    expect(pagina.locator("h1")).to_be_visible()
    evidencia(pagina, "detalle_producto")
    sin_errores_js(pagina)


def test_P03_producto_lleva_al_cotizador(pagina, evidencia):
    pagina.goto(BASE + "/producto/56/")
    expect(pagina.locator("h1")).to_contain_text("Plato")
    pagina.locator("a[href^='/cotizador/?articulo=56']").first.click()
    expect(pagina).to_have_url(re.compile(r"/cotizador/\?articulo=56"))
    expect(pagina.get_by_text("Artículo seleccionado", exact=False)).to_be_attached()
    evidencia(pagina, "cotizador_con_articulo")


def test_P04_producto_inexistente_muestra_404(pagina, evidencia):
    r = pagina.goto(BASE + "/producto/999999/")
    assert r.status == 404
    evidencia(pagina, "pagina_404")


def test_P05_nosotros_y_contacto_con_preguntas_frecuentes(pagina, evidencia):
    pagina.goto(BASE + "/nosotros/")
    expect(pagina.locator("h1, h2").first).to_be_visible()
    evidencia(pagina, "nosotros")
    pagina.goto(BASE + "/contacto/")
    preguntas = pagina.locator("button:has-text('¿')")
    assert preguntas.count() >= 5
    preguntas.nth(0).click()
    pagina.wait_for_timeout(500)
    preguntas.nth(1).click()
    evidencia(pagina, "preguntas_frecuentes")
    expect(pagina.locator("a[href^='tel:']").first).to_be_visible()
    expect(pagina.locator("a[href^='mailto:']").first).to_be_visible()


# ---------------------------------------------------------------- cotizador
def _paso1(pagina):
    pagina.locator("label.option-card", has_text="Matrimonio").click()
    pagina.fill("#fecha-evento", FECHA_FUTURA)
    pagina.fill("#num-invitados", "80")
    pagina.fill("#comuna", "Maipú")
    pagina.locator("label.option-card", has_text="Sí").first.click()
    pagina.locator(".wizard-step[data-step='1'] [data-next]").click()
    expect(pagina.locator(".wizard-step[data-step='2']")).to_have_class(re.compile("is-active"))


def test_P06_cotizador_campos_vacios_no_avanza(pagina, evidencia):
    """Caso de error: Siguiente sin completar ningún dato."""
    pagina.goto(BASE + "/cotizador/")
    pagina.locator(".wizard-step[data-step='1'] [data-next]").click()
    errores = pagina.locator(".wizard-step[data-step='1'] .field-error:visible")
    assert errores.count() >= 4
    expect(pagina.locator(".wizard-step[data-step='1']")).to_have_class(re.compile("is-active"))
    evidencia(pagina, "validaciones_paso1")
    sin_errores_js(pagina)


def test_P07_cotizador_invitados_cero_no_avanza(pagina, evidencia):
    pagina.goto(BASE + "/cotizador/")
    pagina.locator("label.option-card", has_text="Matrimonio").click()
    pagina.fill("#fecha-evento", FECHA_FUTURA)
    pagina.fill("#num-invitados", "0")
    pagina.fill("#comuna", "Maipú")
    pagina.locator("label.option-card", has_text="No, retiro yo").click()
    pagina.locator(".wizard-step[data-step='1'] [data-next]").click()
    expect(pagina.locator(".wizard-step[data-step='1']")).to_have_class(re.compile("is-active"))
    evidencia(pagina, "invitados_cero")


def test_P08_cotizador_paso2_exige_categoria(pagina, evidencia):
    pagina.goto(BASE + "/cotizador/")
    _paso1(pagina)
    pagina.locator(".wizard-step[data-step='2'] [data-next]").click()
    expect(pagina.locator(".wizard-step[data-step='2']")).to_have_class(re.compile("is-active"))
    assert pagina.locator(".wizard-step[data-step='2'] .field-error:visible").count() >= 1
    evidencia(pagina, "categoria_obligatoria")


def test_P09_cotizador_rut_invalido_en_paso3(pagina, evidencia):
    pagina.goto(BASE + "/cotizador/")
    _paso1(pagina)
    pagina.locator("label.checkbox-card", has_text="Vajilla").click()
    pagina.locator(".wizard-step[data-step='2'] [data-next]").click()
    pagina.fill("#nombre", "PRUEBA Automática")
    pagina.fill("#rut", "11.111.111-9")
    pagina.fill("#telefono", "+56 9 1234 5678")
    pagina.click("#btn-submit-quote")
    pagina.wait_for_timeout(1500)
    assert pagina.locator(".wizard-step[data-step='3'] .field-error:visible").count() >= 1
    evidencia(pagina, "rut_invalido")


def test_P10_cotizador_solicitud_completa_valida(pagina, evidencia):
    """Caso válido: se envía una solicitud completa y el sistema confirma."""
    pagina.goto(BASE + "/cotizador/")
    _paso1(pagina)
    evidencia(pagina, "paso1_completo")
    pagina.locator("label.checkbox-card", has_text="Vajilla").click()
    pagina.locator("label.checkbox-card", has_text="Cristal").click()
    evidencia(pagina, "paso2_categorias")
    pagina.locator(".wizard-step[data-step='2'] [data-next]").click()
    pagina.fill("#nombre", f"PRUEBA Automática {SUFIJO}")
    pagina.fill("#rut", RUT_PRUEBA)
    pagina.fill("#telefono", "+56 9 1234 5678")
    pagina.fill("#correo", CORREO_PRUEBA)
    pagina.fill("#direccion", "Calle Prueba 123")
    pagina.locator("label.option-card", has_text="WhatsApp").click()
    pagina.fill("#comentarios", "Solicitud generada por la prueba automática.")
    evidencia(pagina, "paso3_datos")
    pagina.click("#btn-submit-quote")
    expect(pagina.locator("#wizard-confirmation")).to_be_visible(timeout=60000)
    evidencia(pagina, "confirmacion")
    sin_errores_js(pagina)


# ---------------------------------------------------------------- cuenta cliente
def test_P11_registro_campos_vacios(pagina, evidencia):
    pagina.goto(BASE + "/cuenta/registro/")
    pagina.get_by_role("button", name="Crear cuenta").click()
    expect(pagina).to_have_url(re.compile("/cuenta/registro"))
    evidencia(pagina, "registro_vacio")


def test_P12_registro_claves_distintas(pagina, evidencia):
    pagina.goto(BASE + "/cuenta/registro/")
    pagina.fill("input[name=nombre]", "PRUEBA Claves")
    pagina.fill("input[name=rut]", rut_con_dv(13000000 + int(SUFIJO[-6:])))
    pagina.fill("input[name=telefono]", "+56 9 8765 4321")
    pagina.fill("input[name=email]", f"claves.{SUFIJO}@example.com")
    pagina.fill("input[name=password]", CLAVE_PRUEBA)
    pagina.fill("input[name=password2]", "OtraClave2026!")
    pagina.get_by_role("button", name="Crear cuenta").click()
    pagina.wait_for_timeout(1500)
    expect(pagina).to_have_url(re.compile("/cuenta/registro"))
    evidencia(pagina, "claves_distintas")


def test_P13_registro_valido_y_acceso_a_mi_cuenta(pagina, evidencia):
    pagina.goto(BASE + "/cuenta/registro/")
    pagina.fill("input[name=nombre]", f"PRUEBA Cliente {SUFIJO}")
    pagina.fill("input[name=rut]", rut_con_dv(14000000 + int(SUFIJO[-6:])))
    pagina.fill("input[name=telefono]", "+56 9 1111 2222")
    pagina.fill("input[name=email]", CORREO_PRUEBA)
    pagina.fill("input[name=password]", CLAVE_PRUEBA)
    pagina.fill("input[name=password2]", CLAVE_PRUEBA)
    evidencia(pagina, "registro_completo")
    pagina.get_by_role("button", name="Crear cuenta").click()
    pagina.wait_for_url(re.compile(r"/cuenta/?$"), timeout=60000)
    evidencia(pagina, "mi_cuenta")
    sin_errores_js(pagina)


def test_P14_login_con_clave_incorrecta(pagina, evidencia):
    pagina.goto(BASE + "/gestion/login/")
    pagina.fill("#login-correo", "no.existe@example.com")
    pagina.fill("#login-clave", "ClaveIncorrecta1!")
    pagina.get_by_role("button", name="Iniciar sesión").click()
    pagina.wait_for_timeout(1500)
    expect(pagina).to_have_url(re.compile("/gestion/login"))
    evidencia(pagina, "login_rechazado")


# ---------------------------------------------------------------- celular
@pytest.mark.movil
def test_P15_movil_inicio_catalogo_y_cotizador(pagina_movil, evidencia):
    p = pagina_movil
    p.goto(BASE + "/")
    evidencia(p, "inicio_movil")
    p.goto(BASE + "/catalogo/")
    evidencia(p, "catalogo_movil")
    p.goto(BASE + "/cotizador/")
    ancho = p.evaluate("document.documentElement.scrollWidth")
    assert ancho <= 400, f"Hay desplazamiento horizontal en celular ({ancho}px)"
    evidencia(p, "cotizador_movil")
