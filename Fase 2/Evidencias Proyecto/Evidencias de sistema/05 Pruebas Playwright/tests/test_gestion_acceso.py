"""Acceso, seguridad y navegación del panel de gestión."""
import re
import pytest
from playwright.sync_api import expect
from conftest import BASE, ENV
from ayudas import ir, sin_errores_js

RUTAS = ["/gestion/", "/gestion/inventario/", "/gestion/clientes/", "/gestion/cotizaciones/",
         "/gestion/reservas/", "/gestion/preparacion/", "/gestion/despachos/",
         "/gestion/devoluciones/", "/gestion/mermas/", "/gestion/calendario/",
         "/gestion/reportes/", "/gestion/usuarios/", "/gestion/configuracion/"]


def test_G01_rutas_de_gestion_exigen_sesion(pagina, evidencia):
    """Sin iniciar sesión, cada módulo de gestión redirige al login."""
    for ruta in RUTAS:
        pagina.goto(BASE + ruta)
        expect(pagina).to_have_url(re.compile(r"/gestion/login/"))
    evidencia(pagina, "redirige_al_login")
    pagina.goto(BASE + "/cuenta/")
    expect(pagina).to_have_url(re.compile(r"/gestion/login/"))


def test_G02_login_con_clave_incorrecta_es_rechazado(pagina, evidencia):
    pagina.goto(BASE + "/gestion/login/")
    pagina.fill("#login-correo", ENV["ADMIN_EMAIL"])
    pagina.fill("#login-clave", "ClaveIncorrecta-123")
    pagina.get_by_role("button", name="Iniciar sesión").click()
    pagina.wait_for_timeout(2000)
    expect(pagina).to_have_url(re.compile(r"/gestion/login"))
    assert "Resumen general" not in pagina.inner_text("body")
    evidencia(pagina, "clave_incorrecta")


def test_G03_login_campos_vacios(pagina, evidencia):
    pagina.goto(BASE + "/gestion/login/")
    pagina.get_by_role("button", name="Iniciar sesión").click()
    expect(pagina).to_have_url(re.compile(r"/gestion/login"))
    evidencia(pagina, "login_vacio")


def test_G04_login_correcto_mostrar_clave_y_cerrar_sesion(pagina, evidencia):
    pagina.goto(BASE + "/gestion/login/")
    pagina.fill("#login-correo", ENV["ADMIN_EMAIL"])
    # «Mostrar clave» se prueba con un texto de ejemplo para no exponer la clave real en el video.
    pagina.fill("#login-clave", "clave-de-ejemplo")
    pagina.click("#toggle-password")
    assert pagina.get_attribute("#login-clave", "type") == "text"
    pagina.click("#toggle-password")
    assert pagina.get_attribute("#login-clave", "type") == "password"
    pagina.fill("#login-clave", ENV["ADMIN_PASSWORD"])  # el campo queda oculto (••••)
    pagina.get_by_role("button", name="Iniciar sesión").click()
    pagina.wait_for_url(re.compile(r"/gestion/?$"), timeout=60000)
    expect(pagina.locator("body")).to_contain_text("Resumen")
    evidencia(pagina, "panel_tras_login")
    pagina.goto(BASE + "/gestion/logout/")
    pagina.goto(BASE + "/gestion/")
    expect(pagina).to_have_url(re.compile(r"/gestion/login/"))
    evidencia(pagina, "sesion_cerrada")


def test_G05_recorrido_de_todos_los_modulos(admin, evidencia):
    """Entra a cada módulo desde el menú lateral y comprueba que carga sin errores."""
    admin.goto(BASE + "/gestion/")
    expect(admin).to_have_title(re.compile("Resumen general"))
    evidencia(admin, "resumen")
    menu = [("Clientes", "Clientes"), ("Inventario", "Inventario"), ("Cotizaciones", "Cotizaciones"),
            ("Reservas", "Reservas"), ("Preparación de pedidos", "Preparación"), ("Despachos", "Despachos"),
            ("Devoluciones", "Devoluciones"), ("Mermas", "Mermas"), ("Calendario", "Calendario"),
            ("Reportes", "Reportes"), ("Usuarios", "Usuarios"), ("Configuración", "Configuración")]
    for etiqueta, titulo in menu:
        admin.get_by_role("link", name=etiqueta, exact=True).first.click()
        expect(admin).to_have_title(re.compile(titulo))
        expect(admin.locator("body")).not_to_contain_text("Server Error")
        evidencia(admin, f"modulo_{titulo}")
    sin_errores_js(admin)


def test_G06_identificadores_inexistentes_devuelven_404(admin, evidencia):
    for ruta in ["/gestion/clientes/999999/", "/gestion/cotizaciones/999999/", "/gestion/reservas/999999/",
                 "/gestion/inventario/999999/", "/gestion/preparacion/999999/", "/gestion/despachos/999999/"]:
        r = admin.goto(BASE + ruta)
        assert r.status == 404, f"{ruta} -> {r.status}"
    evidencia(admin, "pagina_404")


def test_G07_formulario_sin_token_csrf_es_rechazado(admin, evidencia):
    """Un envío POST sin token de seguridad no debe modificar datos."""
    admin.goto(BASE + "/gestion/clientes/")
    r = admin.request.post(BASE + "/gestion/clientes/", form={"accion": "crear", "nombre": "SinToken"})
    assert r.status in (403, 400), f"Se esperaba rechazo y se obtuvo {r.status}"
    admin.goto(BASE + "/gestion/clientes/?q=SinToken")
    expect(admin.locator("body")).not_to_contain_text("SinToken ")
    evidencia(admin, "csrf_rechazado")


def test_G08_busqueda_global_del_panel(admin, evidencia):
    """El servicio de búsqueda responde con resultados agrupados.

    Observación: en la interfaz la caja de búsqueda superior está oculta por CSS
    (.topbar-search { display: none }), por eso se prueba el servicio directamente.
    """
    admin.goto(BASE + "/gestion/clientes/")
    r = admin.request.get(BASE + "/gestion/buscar/?q=vajilla")
    assert r.status == 200
    datos = r.json()
    assert datos["grupos"] and datos["grupos"][0]["items"], "La búsqueda no devolvió resultados"
    admin.goto(BASE + "/gestion/buscar/?q=vajilla")
    evidencia(admin, "respuesta_busqueda")
    vacio = admin.request.get(BASE + "/gestion/buscar/?q=zzzzzz-no-existe").json()
    assert not any(g["items"] for g in vacio["grupos"])


@pytest.mark.movil
def test_G09_panel_en_celular(browser, estado_admin, request, evidencia):
    from conftest import _nuevo, MOVIL
    gen = _nuevo(browser, request, storage=estado_admin, viewport=MOVIL)
    p = next(gen)
    try:
        p.goto(BASE + "/gestion/")
        evidencia(p, "resumen_movil")
        p.locator("#sidebar-toggle").click()
        p.wait_for_timeout(800)
        evidencia(p, "menu_movil")
        ancho = p.evaluate("document.documentElement.scrollWidth")
        assert ancho <= 400, f"Desplazamiento horizontal en celular: {ancho}px"
        p.goto(BASE + "/gestion/inventario/")
        evidencia(p, "inventario_movil")
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
