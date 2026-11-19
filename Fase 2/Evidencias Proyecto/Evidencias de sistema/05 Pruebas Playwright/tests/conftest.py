"""Configuración común: sitio en Render, grabación de video y evidencias.

Cada prueba abre su propio navegador (visible en el video), con movimiento
pausado (slow_mo) para que la grabación se entienda a simple vista.
Las credenciales se leen de .env.local (no se versiona ni se muestra).
"""
import os, re, shutil, time, itertools
from pathlib import Path
import pytest
from playwright.sync_api import expect

RAIZ = Path(__file__).resolve().parent.parent
VIDEOS = RAIZ / "videos"
EVID = RAIZ / "evidencias"
TMP = RAIZ / "test-results"
for d in (VIDEOS, EVID, TMP, RAIZ / "informe"):
    d.mkdir(exist_ok=True)


def _env():
    datos = {}
    f = RAIZ / ".env.local"
    if f.exists():
        for linea in f.read_text(encoding="utf-8").splitlines():
            if "=" in linea:
                k, v = linea.split("=", 1)
                datos[k.strip()] = v.strip()
    for k in ("BASE_URL", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        datos[k] = os.environ.get(k, datos.get(k, ""))
    return datos


ENV = _env()
BASE = ENV["BASE_URL"].rstrip("/")
SLOW_MO = int(os.environ.get("SLOW_MO", "350"))
VIEWPORT = {"width": 1366, "height": 768}
MOVIL = {"width": 390, "height": 844}
SUFIJO = time.strftime("%m%d%H%M%S")


def rut_con_dv(cuerpo: int) -> str:
    """RUT válido (con dígito verificador) a partir del número base."""
    suma, mult = 0, 2
    for d in reversed(str(cuerpo)):
        suma += int(d) * mult
        mult = 2 if mult == 7 else mult + 1
    r = 11 - suma % 11
    dv = "0" if r == 11 else "K" if r == 10 else str(r)
    return f"{cuerpo}-{dv}"


def pytest_configure(config):
    config.addinivalue_line("markers", "movil: prueba en pantalla de celular")


@pytest.fixture(scope="session")
def base():
    return BASE


@pytest.fixture(scope="session", autouse=True)
def despertar_sitio():
    """Render gratuito se duerme: espera a que responda antes de empezar."""
    import urllib.request
    limite = time.time() + 240
    while time.time() < limite:
        try:
            with urllib.request.urlopen(BASE + "/", timeout=60) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(5)
    pytest.exit("El sitio no respondió en 4 minutos")


@pytest.fixture(scope="session")
def estado_admin(browser, despertar_sitio):
    """Inicia sesión una vez y guarda la sesión para las pruebas de gestión."""
    ruta = TMP / "estado_admin.json"
    ctx = browser.new_context(viewport=VIEWPORT)
    p = ctx.new_page()
    p.set_default_timeout(60000)
    p.goto(BASE + "/gestion/login/")
    p.fill("#login-correo", ENV["ADMIN_EMAIL"])
    p.fill("#login-clave", ENV["ADMIN_PASSWORD"])
    p.click("button[type=submit]")
    p.wait_for_url(re.compile(r"/gestion/?$"), timeout=60000)
    ctx.storage_state(path=str(ruta))
    ctx.close()
    return str(ruta)


@pytest.fixture(scope="session")
def browser_type_launch_args():
    return {"slow_mo": SLOW_MO}


def _nuevo(browser, request, storage=None, viewport=VIEWPORT):
    nombre = re.sub(r"[^\w]+", "_", request.node.name)
    carpeta = TMP / nombre
    shutil.rmtree(carpeta, ignore_errors=True)
    carpeta.mkdir(parents=True)
    args = dict(viewport=viewport, record_video_dir=str(carpeta),
                record_video_size=viewport, locale="es-CL",
                accept_downloads=True, ignore_https_errors=True)
    if storage:
        args["storage_state"] = storage
    ctx = browser.new_context(**args)
    page = ctx.new_page()
    page.set_default_timeout(45000)
    page.set_default_navigation_timeout(90000)
    page.consola = []
    page.on("console", lambda m: page.consola.append(m) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.consola.append(e))
    yield page
    video = page.video
    fallo = getattr(request.node, "rep_call", None) is not None and request.node.rep_call.failed
    if fallo:
        try:
            page.screenshot(path=str(EVID / f"{nombre}__FALLO.png"), full_page=True)
        except Exception:
            pass
    ctx.close()
    try:
        destino = VIDEOS / f"{nombre}.webm"
        destino.unlink(missing_ok=True)
        shutil.move(video.path(), destino)
    except Exception:
        pass


@pytest.fixture
def pagina(browser, request, despertar_sitio):
    """Visitante sin sesión (cliente público)."""
    yield from _nuevo(browser, request)


@pytest.fixture
def pagina_movil(browser, request, despertar_sitio):
    yield from _nuevo(browser, request, viewport=MOVIL)


@pytest.fixture
def admin(browser, request, estado_admin):
    """Administrador con sesión iniciada."""
    yield from _nuevo(browser, request, storage=estado_admin)


@pytest.fixture
def evidencia(request):
    contador = itertools.count(1)
    nombre = re.sub(r"[^\w]+", "_", request.node.name)

    def _tomar(page, descripcion):
        archivo = EVID / f"{nombre}__{next(contador):02d}_{re.sub(r'[^\w]+', '_', descripcion)[:40]}.png"
        page.screenshot(path=str(archivo), full_page=False)
        return archivo
    return _tomar


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    resultado = yield
    rep = resultado.get_result()
    setattr(item, "rep_" + rep.when, rep)
