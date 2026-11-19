"""Funciones auxiliares para las pruebas del panel de gestión."""
import re
from playwright.sync_api import expect
from conftest import BASE


def sin_errores_js(page):
    errores = [str(e) for e in page.consola if "favicon" not in str(e) and "wa.me" not in str(e)]
    assert not errores, f"Errores de consola/JS: {errores[:3]}"


def ir(page, ruta):
    resp = page.goto(BASE + ruta)
    assert resp.status < 400, f"{ruta} respondió {resp.status}"
    return resp


def clic_y_confirmar(page, nombre, espera=3000):
    """Pulsa un botón; si el sistema pide confirmación, confirma con el mismo texto."""
    boton = page.get_by_role("button", name=nombre).first
    boton.click()
    page.wait_for_timeout(900)
    todos = page.get_by_role("button", name=nombre)
    if todos.count() > 1 and todos.last.is_visible():
        todos.last.click()
    page.wait_for_timeout(espera)


def mensaje_flash(page):
    """Texto de la notificación superior (éxito o error), si existe."""
    for sel in [".flash", ".alert", ".toast", "[role=alert]", ".mensaje", ".messages li"]:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            return loc.first.inner_text()
    return ""


def abrir_modal(page, texto_boton):
    page.get_by_role("button", name=texto_boton).first.click()
    page.wait_for_timeout(700)
