from datetime import date, datetime
from functools import wraps
import json
import logging
import re
import unicodedata
from urllib.parse import quote
from uuid import uuid4

from django.contrib import messages
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.db.models import Sum, Avg, Count, F, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from Inventario.disponibilidad import (
    ESTADOS_QUE_BLOQUEAN,
    StockInsuficiente,
    confirmar_servicio,
    disponibilidad_servicio,
    periodo_servicio,
    calcular_disponibilidad,
)
from Inventario.validaciones import (
    correo_valido,
    normalizar_rut,
    rut_valido,
    telefono_valido,
)

from Inventario.models import (
    Negocio,
    Categoria,
    Articulo,
    Cliente,
    Servicio,
    DetalleServicio,
    Preparacion,
    DetallePreparacion,
    Despacho,
    DetalleDespacho,
    Devolucion,
    DetalleDevolucion,
    Merma,
    MovimientoInventario,
    Rol,
    Usuario,
)


logger = logging.getLogger(__name__)

# Servicios que ya son reservas (confirmadas, en curso o devueltas); excluye cotizaciones y cancelados.
ESTADOS_RESERVA = [*ESTADOS_QUE_BLOQUEAN, Servicio.Estado.FINALIZADO]


# UTILIDADES

def obtener_negocio():
    return Negocio.objects.filter(activo=True).order_by("id").first()


def _separar_nombre(nombre_completo):
    partes = nombre_completo.strip().split()

    if not partes:
        return "", ""

    if len(partes) == 1:
        return partes[0], ""

    return partes[0], " ".join(partes[1:])


def _entero(valor, default=0):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


# SITIO PÚBLICO

def index(request):
    return render(request, "index.html")


def catalogo(request):
    productos = list(
        Articulo.objects
        .filter(activo=True)
        .select_related("categoria")
        .order_by("nombre")
    )

    categorias = list(
        Categoria.objects
        .filter(activo=True, articulos__activo=True)
        .distinct()
        .order_by("nombre")
    )

    return render(
        request,
        "catalogo.html",
        {
            "productos": productos,
            "categorias": categorias,
        },
    )


TIPOS_EVENTO = [
    "Matrimonio",
    "Cumpleaños",
    "Evento empresarial",
    "Cena / Banquete",
    "Evento familiar",
    "Celebración",
    "Otro",
]

ETIQUETAS_CATEGORIA = {
    "vajilla": "Vajilla",
    "cristaleria": "Cristalería",
    "cubiertos": "Cubiertos",
    "manteleria": "Mantelería",
    "mesas-sillas": "Mesas y sillas",
    "decoracion": "Decoración",
    "equipamiento": "Equipamiento",
    "carpas-exteriores": "Carpas",
}

CATEGORIA_POR_DEFINIR = "por-definir"

MENSAJE_SOLICITUD_ENVIADA = (
    "Tu solicitud de cotización fue enviada correctamente. "
    "Nos comunicaremos contigo por WhatsApp o correo."
)

MENSAJE_ERROR_INTERNO = (
    "No pudimos registrar tu solicitud. Inténtalo nuevamente en unos minutos."
)


def etiqueta_categoria(categoria):
    return ETIQUETAS_CATEGORIA.get(
        categoria.nombre,
        categoria.nombre.replace("-", " ").capitalize(),
    )


def _quiere_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _validar_solicitud(post, categorias):
    """Valida y limpia los datos del cotizador. Devuelve (errores, datos)."""
    errores = {}
    datos = {}

    tipo_evento = post.get("tipoEvento", "").strip()
    if tipo_evento not in TIPOS_EVENTO:
        errores["tipoEvento"] = "Selecciona un tipo de evento."
    datos["tipo_evento"] = tipo_evento

    try:
        fecha_evento = date.fromisoformat(post.get("fecha", "").strip())
    except ValueError:
        fecha_evento = None
        errores["fecha"] = "Selecciona una fecha válida."
    else:
        if fecha_evento < date.today():
            errores["fecha"] = "La fecha no puede ser anterior a hoy."
    datos["fecha_evento"] = fecha_evento

    invitados = _entero(post.get("invitados", "").strip(), 0)
    if invitados < 1:
        errores["invitados"] = "Ingresa una cantidad de invitados mayor que cero."
    elif invitados > 10000:
        errores["invitados"] = "Ingresa una cantidad de invitados realista."
    datos["invitados"] = invitados

    comuna = post.get("comuna", "").strip()
    if not comuna:
        errores["comuna"] = "Ingresa la comuna."
    datos["comuna"] = comuna[:100]

    despacho = post.get("despacho", "").strip()
    if despacho not in ("Sí", "No, retiro yo"):
        errores["despacho"] = "Indica si requieres despacho."
    datos["requiere_despacho"] = despacho == "Sí"

    por_id = {str(c.id): c for c in categorias}
    elegidas = post.getlist("categorias")
    desconocidas = [v for v in elegidas if v != CATEGORIA_POR_DEFINIR and v not in por_id]
    if not elegidas:
        errores["categorias"] = 'Selecciona al menos una categoría o "Aún no estoy seguro/a".'
    elif desconocidas:
        errores["categorias"] = "Selecciona categorías válidas."
    # "Aún no estoy seguro/a" es excluyente: si viene, se ignoran las demás.
    datos["categorias_por_definir"] = CATEGORIA_POR_DEFINIR in elegidas
    datos["categorias"] = (
        [] if datos["categorias_por_definir"]
        else [por_id[v] for v in elegidas if v in por_id]
    )

    nombre_completo = post.get("nombre", "").strip()
    if not nombre_completo:
        errores["nombre"] = "Ingresa tu nombre."
    datos["nombre"], datos["apellido"] = _separar_nombre(nombre_completo[:200])

    rut = normalizar_rut(post.get("rut", ""))
    if not rut:
        errores["rut"] = "Ingresa tu RUT."
    elif not rut_valido(rut):
        errores["rut"] = "El RUT no es válido. Revisa el número y el dígito verificador."
    datos["rut"] = rut

    telefono = post.get("telefono", "").strip()
    if telefono and not telefono_valido(telefono):
        errores["telefono"] = "Ingresa un teléfono válido o déjalo en blanco."
    datos["telefono"] = telefono[:30]

    correo = post.get("correo", "").strip().lower()
    if correo and not correo_valido(correo):
        errores["correo"] = "Ingresa un correo válido o déjalo en blanco."
    datos["correo"] = correo[:150]

    medio = post.get("medioContacto", "").strip()
    if medio and medio not in Servicio.MedioContacto.values:
        errores["medioContacto"] = "Selecciona un medio de contacto válido."
    datos["medio_contacto"] = medio

    datos["direccion"] = post.get("direccion", "").strip()[:255]
    datos["empresa"] = post.get("empresa", "").strip()[:150]
    datos["comentarios"] = post.get("comentarios", "").strip()[:2000]

    return errores, datos


def _buscar_cliente_por_rut(negocio, rut, bloquear=False):
    clientes = Cliente.objects.select_for_update() if bloquear else Cliente.objects
    cliente = clientes.filter(negocio=negocio, rut=rut).first()

    # Registros antiguos que todavía tengan el RUT con puntos o guion.
    if cliente is None:
        cliente = next(
            (
                c for c in Cliente.objects.filter(negocio=negocio).exclude(rut="")
                if normalizar_rut(c.rut) == rut
            ),
            None,
        )
    return cliente


def _palabras_nombre(texto):
    """Palabras de un nombre en minúsculas y sin tildes, para comparar nombres escritos distinto."""
    sin_tildes = unicodedata.normalize("NFD", texto or "").encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z]+", sin_tildes.lower()))


def _nombre_coincide(cliente, nombre_completo):
    """El nombre escrito coincide si uno contiene todas las palabras del otro
    (permite "Ana Pérez" frente a "Ana María Pérez Soto", pero no "Juan Soto")."""
    registrado = _palabras_nombre(f"{cliente.nombre} {cliente.apellido}")
    escrito = _palabras_nombre(nombre_completo)
    if not registrado or not escrito:
        return True
    return registrado <= escrito or escrito <= registrado


def _obtener_o_crear_cliente(negocio, datos):
    """Busca al cliente por RUT normalizado; si existe solo completa los datos opcionales nuevos."""
    cliente = _buscar_cliente_por_rut(negocio, datos["rut"], bloquear=True)

    if cliente is None:
        return Cliente.objects.create(
            negocio=negocio,
            rut=datos["rut"],
            nombre=datos["nombre"],
            apellido=datos["apellido"],
            telefono=datos["telefono"],
            email=datos["correo"],
            direccion=datos["direccion"],
            comuna=datos["comuna"],
            activo=True,
        )

    cliente.rut = datos["rut"]
    cliente.telefono = datos["telefono"] or cliente.telefono
    cliente.email = datos["correo"] or cliente.email
    cliente.direccion = datos["direccion"] or cliente.direccion
    cliente.comuna = cliente.comuna or datos["comuna"]
    cliente.activo = True
    cliente.save()
    return cliente


def _registrar_solicitud(negocio, datos, articulo_consultado=None):
    """Crea (o reutiliza) el cliente y crea el servicio con sus categorías en una sola transacción."""
    notas = []
    if datos["empresa"]:
        notas.append(f"Empresa: {datos['empresa']}")
    if articulo_consultado:
        notas.append(
            f"Artículo consultado desde el catálogo: "
            f"{articulo_consultado.codigo} - {articulo_consultado.nombre}"
        )

    with transaction.atomic():
        cliente = _obtener_o_crear_cliente(negocio, datos)

        servicio = Servicio.objects.create(
            negocio=negocio,
            cliente=cliente,
            codigo=f"COT-{date.today():%Y%m%d}-{uuid4().hex[:6].upper()}",
            estado=Servicio.Estado.COTIZACION,
            tipo_servicio=datos["tipo_evento"],
            fecha_solicitud=date.today(),
            fecha_evento=datos["fecha_evento"],
            fecha_inicio=datos["fecha_evento"],
            fecha_termino=datos["fecha_evento"],
            direccion_evento=datos["direccion"],
            comuna_evento=datos["comuna"],
            cantidad_personas=datos["invitados"],
            requiere_despacho=datos["requiere_despacho"],
            medio_contacto=datos["medio_contacto"],
            comentarios=datos["comentarios"],
            categorias_por_definir=datos["categorias_por_definir"],
            observaciones="\n".join(notas),
        )
        servicio.categorias_solicitadas.set(datos["categorias"])

    return servicio


def cotizador(request):
    negocio = obtener_negocio()

    if negocio is None:
        return render(
            request,
            "cotizador.html",
            {
                "categorias": [],
                "error": "El cotizador no está disponible en este momento.",
            },
        )

    categorias = list(
        Categoria.objects
        .filter(negocio=negocio, activo=True)
        .order_by("nombre")
    )
    for categoria in categorias:
        categoria.etiqueta = etiqueta_categoria(categoria)

    articulo_id = request.GET.get("articulo") or request.POST.get("articulo_id")
    articulo_preseleccionado = None
    if articulo_id and str(articulo_id).isdigit():
        articulo_preseleccionado = (
            Articulo.objects
            .filter(id=articulo_id, negocio=negocio, activo=True)
            .first()
        )

    contexto = {
        "categorias": categorias,
        "categoria_por_definir": CATEGORIA_POR_DEFINIR,
        "articulo_preseleccionado": articulo_preseleccionado,
    }

    if request.method != "POST":
        return render(request, "cotizador.html", contexto)

    errores, datos = _validar_solicitud(request.POST, categorias)

    # Un RUT ya registrado no puede usarse con el nombre de otra persona.
    if not errores:
        existente = _buscar_cliente_por_rut(negocio, datos["rut"])
        if existente and not _nombre_coincide(existente, request.POST.get("nombre", "")):
            errores["rut"] = (
                "Este RUT ya está registrado con otro nombre. "
                "Revisa que tu nombre y tu RUT estén bien escritos."
            )

    if errores:
        mensaje = "Revisa los campos marcados antes de enviar."
        if _quiere_json(request):
            return JsonResponse({"ok": False, "message": mensaje, "errors": errores}, status=400)
        return render(request, "cotizador.html", {**contexto, "error": mensaje, "errores": errores})

    try:
        _registrar_solicitud(negocio, datos, articulo_preseleccionado)
    except Exception:
        logger.exception("Error al registrar una solicitud de cotización")
        if _quiere_json(request):
            return JsonResponse({"ok": False, "message": MENSAJE_ERROR_INTERNO}, status=500)
        return render(request, "cotizador.html", {**contexto, "error": MENSAJE_ERROR_INTERNO})

    if _quiere_json(request):
        return JsonResponse({"ok": True, "message": MENSAJE_SOLICITUD_ENVIADA})
    return render(request, "cotizador.html", {**contexto, "enviado": True, "mensaje": MENSAJE_SOLICITUD_ENVIADA})


def nosotros(request):
    return render(request, "nosotros.html")


def contacto(request):
    return render(request, "contacto.html")


def producto(request, id):
    producto = get_object_or_404(
        Articulo.objects.select_related("categoria", "negocio"),
        id=id,
        activo=True,
    )

    relacionados = list(
        Articulo.objects
        .filter(
            activo=True,
            categoria=producto.categoria,
        )
        .exclude(id=producto.id)
        .select_related("categoria")[:4]
    )

    return render(
        request,
        "producto.html",
        {
            "producto": producto,
            "relacionados": relacionados,
        },
    )


def solo_administrador(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):

        usuario_id = request.session.get("usuario_id")

        # No ha iniciado sesión
        if not usuario_id:
            login_url = reverse("gestion_login")

            return redirect(
                f"{login_url}?next={request.get_full_path()}"
            )

        usuario = (
            Usuario.objects
            .filter(
                id=usuario_id,
                activo=True
            )
            .select_related(
                "rol",
                "negocio"
            )
            .first()
        )

        # La sesión apunta a un usuario que ya no existe o fue desactivado.
        if usuario is None:
            request.session.flush()

            return redirect("gestion_login")

        # Solo Administrador
        if (
            not usuario.rol
            or usuario.rol.nombre != "Administrador"
        ):
            return HttpResponseForbidden(
                "No tienes permisos para acceder a esta sección."
            )

        # Dejamos el usuario disponible para la vista
        request.usuario_panel = usuario

        return view_func(
            request,
            *args,
            **kwargs
        )

    return wrapper

# LOGIN

def gestion_login(request):
    if request.method == "POST":
        email = request.POST.get(
            "email",
            "",
        ).strip().lower()

        password = request.POST.get(
            "password",
            "",
        )

        usuario = (
            Usuario.objects
            .filter(
                email=email,
                activo=True,
            )
            .select_related(
                "rol",
                "negocio",
            )
            .first()
        )

        if usuario:
            password_valida = False

            try:
                password_valida = check_password(
                    password,
                    usuario.password,
                )
            except Exception:
                password_valida = False

            # Compatibilidad temporal con registros antiguos en texto plano.
            if not password_valida:
                password_valida = (
                    usuario.password == password
                )

            if password_valida:
                request.session["usuario_id"] = usuario.id
                request.session["usuario_nombre"] = (
                    f"{usuario.nombre} {usuario.apellido}".strip()
                )
                request.session["usuario_rol"] = usuario.rol.nombre

                destino = request.GET.get("next")

                if destino:
                    return redirect(destino)

                return redirect("inicio")

        return render(
            request,
            "login.html",
            {
                "error": "Correo o contraseña incorrectos.",
            },
        )

    return render(
        request,
        "login.html",
    )

@solo_administrador
def gestion_logout(request):
    request.session.flush()
    return redirect("gestion_login")

# GESTIÓN - INICIO
@solo_administrador
def gestionIndex(request):
    negocio = obtener_negocio()
    hoy = date.today()

    if negocio is None:
        return render(
            request,
            "inicio.html",
            {
                "fecha_hoy": hoy,
                "error": "No existe un negocio activo configurado.",
            },
        )

    estados_activos = [
        "Confirmado",
        "Preparacion",
        "Despachado",
        "Devolucion parcial",
    ]

    servicios_negocio = Servicio.objects.filter(
        negocio=negocio
    )

    reservas_activas = servicios_negocio.filter(
        estado__in=estados_activos
    ).count()

    proximos_eventos = list(
        servicios_negocio
        .filter(
            estado__in=estados_activos,
            fecha_evento__gte=hoy,
        )
        .select_related("cliente")
        .order_by("fecha_evento")[:5]
    )

    from datetime import timedelta
    proximos_7 = hoy + timedelta(days=7)

    reservas_proximas_7_dias = servicios_negocio.filter(
        estado__in=estados_activos,
        fecha_evento__gte=hoy,
        fecha_evento__lte=proximos_7,
    ).count()

    articulos_qs = Articulo.objects.filter(
        negocio=negocio,
        activo=True,
    )

    articulos_totales = (
        articulos_qs.aggregate(total=Sum("stock_total"))["total"]
        or 0
    )

    # Disponible = stock trabajable menos lo comprometido en reservas vigentes.
    comprometido = comprometido_vigente(list(articulos_qs.values_list("id", flat=True)))
    articulos_disponibles = sum(
        max(0, a.stock_trabajable - comprometido.get(a.id, 0))
        for a in articulos_qs.only("id", "stock_trabajable")
    )

    alertas_inventario = list(
        articulos_qs
        .filter(stock_trabajable__lte=10)
        .select_related("categoria")
        .order_by("stock_trabajable", "nombre")[:6]
    )

    alertas_inventario_count = articulos_qs.filter(
        stock_trabajable__lte=10
    ).count()

    cotizaciones_qs = servicios_negocio.filter(
        estado="Cotizacion"
    )

    cotizaciones_pendientes = cotizaciones_qs.count()

    cotizaciones_mes = cotizaciones_qs.filter(
        fecha_creacion__year=hoy.year,
        fecha_creacion__month=hoy.month,
    ).count()

    monto_cotizado_mes = (
        cotizaciones_qs
        .filter(
            fecha_creacion__year=hoy.year,
            fecha_creacion__month=hoy.month,
        )
        .aggregate(total=Sum("total"))["total"]
        or 0
    )

    entregas_hoy = Despacho.objects.filter(
        servicio__negocio=negocio,
        fecha_despacho__date=hoy,
    ).count()

    # "Retiros hoy" se interpreta como servicios cuyo arriendo termina hoy.
    retiros_hoy = servicios_negocio.filter(
        fecha_termino=hoy,
    ).exclude(
        estado__in=["Cotizacion", "Contactado", "Cancelado"]
    ).count()

    pendientes_preparar = Preparacion.objects.filter(
        servicio__negocio=negocio,
    ).exclude(
        estado__iexact="Completada"
    ).count()

    devoluciones_pendientes = servicios_negocio.filter(
        estado__in=[
            "Despachado",
            "Devolucion parcial",
        ]
    ).count()

    # No existe una tabla específica de mantención en el modelo actual.
    en_mantencion = 0

    registros_merma = Merma.objects.filter(
        articulo__negocio=negocio
    ).count()

    productos_mas_arrendados = list(
        DetalleServicio.objects
        .filter(servicio__negocio=negocio, servicio__estado__in=ESTADOS_RESERVA)
        .values(
            nombre=F("articulo__nombre")
        )
        .annotate(
            cantidad=Sum("cantidad")
        )
        .order_by("-cantidad")[:5]
    )

    categorias_solicitadas = list(
        DetalleServicio.objects
        .filter(servicio__negocio=negocio)
        .values(
            categoria=F("articulo__categoria__nombre")
        )
        .annotate(
            cantidad=Sum("cantidad")
        )
        .order_by("-cantidad")[:5]
    )

    # Evitamos depender de un related_name particular entre Cliente y Servicio.
    clientes_base = {
        c.id: {
            "nombre": c.nombre,
            "apellido": c.apellido,
            "cantidad": 0,
        }
        for c in Cliente.objects.filter(negocio=negocio)
    }

    conteos_clientes = (
        servicios_negocio
        .exclude(estado__in=["Cotizacion", "Contactado"])
        .values("cliente_id")
        .annotate(cantidad=Count("id"))
        .order_by("-cantidad")[:5]
    )

    clientes_frecuentes = []

    for fila in conteos_clientes:
        dato = clientes_base.get(fila["cliente_id"])
        if dato:
            dato = dato.copy()
            dato["cantidad"] = fila["cantidad"]
            clientes_frecuentes.append(dato)

    # Ingresos finalizados agrupados en Python para mantener compatibilidad.
    servicios_finalizados = (
        servicios_negocio
        .filter(
            estado__in=ESTADOS_RESERVA,
            fecha_evento__isnull=False,
        )
        .values("fecha_evento", "total")
        .order_by("fecha_evento")
    )

    meses = {}

    for item in servicios_finalizados:
        fecha_evento = item["fecha_evento"]
        clave = fecha_evento.strftime("%Y-%m")
        etiqueta = fecha_evento.strftime("%m/%Y")
        meses.setdefault(
            clave,
            {
                "mes": etiqueta,
                "total": 0,
            },
        )
        meses[clave]["total"] += item["total"] or 0

    ingresos_mensuales = [
        meses[k]
        for k in sorted(meses.keys())[-6:]
    ]

    porcentaje_disponible = (
        round(articulos_disponibles * 100 / articulos_totales)
        if articulos_totales
        else 0
    )

    dias_semana = [
        "lunes", "martes", "miércoles", "jueves",
        "viernes", "sábado", "domingo",
    ]
    nombres_meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
        "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    ]
    fecha_hoy_texto = (
        f"{dias_semana[hoy.weekday()]}, {hoy.day} de "
        f"{nombres_meses[hoy.month - 1]}"
    )

    usuario_nombre = request.session.get(
        "usuario_nombre",
        "",
    )

    usuario_rol = request.session.get(
        "usuario_rol",
        "",
    )

    usuario_iniciales = ""

    if usuario_nombre:
        palabras = usuario_nombre.split()
        usuario_iniciales = "".join(
            palabra[0].upper()
            for palabra in palabras[:2]
            if palabra
        )

    return render(
        request,
        "inicio.html",
        {
            "fecha_hoy": hoy,
            "fecha_hoy_texto": fecha_hoy_texto,

            "porcentaje_disponible": porcentaje_disponible,

            "reservas_activas": reservas_activas,
            "reservas_proximas_7_dias": reservas_proximas_7_dias,

            "articulos_totales": articulos_totales,
            "articulos_disponibles": articulos_disponibles,

            "cotizaciones_pendientes": cotizaciones_pendientes,
            "cotizaciones_mes": cotizaciones_mes,
            "monto_cotizado_mes": monto_cotizado_mes,

            "proximos_eventos": proximos_eventos,

            "alertas_inventario": alertas_inventario,
            "alertas_inventario_count": alertas_inventario_count,

            "entregas_hoy": entregas_hoy,
            "retiros_hoy": retiros_hoy,
            "pendientes_preparar": pendientes_preparar,
            "devoluciones_pendientes": devoluciones_pendientes,
            "en_mantencion": en_mantencion,
            "registros_merma": registros_merma,

            "ingresos_mensuales": ingresos_mensuales,
            "categorias_solicitadas": categorias_solicitadas,
            "productos_mas_arrendados": productos_mas_arrendados,
            "clientes_frecuentes": clientes_frecuentes,

            "usuario_nombre": usuario_nombre,
            "usuario_rol": usuario_rol,
            "usuario_iniciales": usuario_iniciales,
        },
    )


# INVENTARIO

PREFIJOS_CATEGORIA = {
    "Vajilla": "VAJ",
    "Cristalería": "CRIS",
    "Cubiertos y servicio": "CUB",
    "Mesas y sillas": "MOB",
    "Mantelería": "MAN",
    "Carpas y exteriores": "CAR",
    "Equipamiento": "EQU",
    "Decoración": "DEC",
}

def generar_codigo_producto(categoria):
    prefijo = PREFIJOS_CATEGORIA.get(
        categoria.nombre,
        categoria.nombre[:3].upper(),
    )

    productos = (
        Articulo.objects
        .filter(codigo__startswith=f"{prefijo}-")
        .values_list("codigo", flat=True)
    )

    numeros = []

    for codigo in productos:
        coincidencia = re.search(r"-(\d+)$", codigo)

        if coincidencia:
            numeros.append(int(coincidencia.group(1)))

    siguiente = (max(numeros) if numeros else 0) + 1

    return f"{prefijo}-{siguiente:03d}"

def _stock_desde_post(post, actual_total=0, actual_trabajable=0):
    """Lee y valida stock total y trabajable. Devuelve (total, trabajable, error)."""
    total = _entero(post.get("stock_total"), -1)
    trabajable = _entero(post.get("stock_trabajable"), -1)
    if total < 0 or trabajable < 0:
        return actual_total, actual_trabajable, "El stock debe ser un número entero igual o mayor que cero."
    if trabajable > total:
        return actual_total, actual_trabajable, "El stock trabajable no puede ser mayor que el stock total."
    return total, trabajable, None


def comprometido_vigente(articulo_ids):
    """Unidades retenidas por reservas activas cuyo evento termina hoy o después, por artículo."""
    hoy = date.today()
    filas = (
        DetalleServicio.objects
        .filter(articulo_id__in=articulo_ids, servicio__estado__in=ESTADOS_QUE_BLOQUEAN)
        .annotate(fin=Coalesce("servicio__fecha_termino", "servicio__fecha_evento", "servicio__fecha_inicio"))
        .filter(fin__gte=hoy)
        .values("articulo_id")
        .annotate(total=Sum("cantidad"))
    )
    return {fila["articulo_id"]: fila["total"] or 0 for fila in filas}


# Relaciones que impiden eliminar un artículo (los movimientos de stock se borran con él).
RELACIONES_ARTICULO_EN_USO = (
    DetalleServicio, DetallePreparacion, DetalleDespacho, DetalleDevolucion, Merma,
)


def _articulos_en_uso(articulo_ids):
    """IDs de los artículos que aparecen en alguna reserva u operación."""
    en_uso = set()
    for modelo in RELACIONES_ARTICULO_EN_USO:
        en_uso.update(
            modelo.objects.filter(articulo_id__in=articulo_ids).values_list("articulo_id", flat=True)
        )
    return en_uso


def _articulo_en_uso(articulo):
    return bool(_articulos_en_uso([articulo.id]))


@solo_administrador
def inventario(request):
    negocio = obtener_negocio()

    if request.method == "POST" and request.POST.get("accion") == "eliminar":
        articulo = get_object_or_404(Articulo, id=request.POST.get("articulo_id"), negocio=negocio)
        # Un producto usado en operaciones no se elimina, para no perder historial.
        if _articulo_en_uso(articulo):
            messages.error(request, f"No se puede eliminar {articulo.nombre}: tiene reservas u operaciones asociadas.")
            return redirect("gestion_inventario")

        nombre = articulo.nombre
        with transaction.atomic():
            articulo.movimientos.all().delete()
            articulo.delete()
        messages.success(request, f"Producto {nombre} eliminado.")
        return redirect("gestion_inventario")

    if request.method == "POST" and request.POST.get("accion") == "crear":
        categoria = get_object_or_404(Categoria, id=request.POST.get("categoria_id"), negocio=negocio)
        nombre = request.POST.get("nombre", "").strip()
        precio = _entero(request.POST.get("precio_arriendo"), -1)
        stock_total, stock_trabajable, error = _stock_desde_post(request.POST)

        if not nombre:
            error = "El nombre del producto es obligatorio."
        elif precio < 0:
            error = "El precio de arriendo debe ser un número igual o mayor que cero."

        codigo = request.POST.get("codigo", "").strip() or generar_codigo_producto(categoria)
        if not error and Articulo.objects.filter(codigo=codigo).exists():
            error = f"Ya existe un producto con el código {codigo}."

        if error:
            messages.error(request, f"No se creó el producto: {error}")
            return redirect("gestion_inventario")

        with transaction.atomic():
            articulo = Articulo.objects.create(
                negocio=negocio,
                categoria=categoria,
                codigo=codigo,
                nombre=nombre,
                categoria_label=request.POST.get("categoria_label", "").strip() or categoria.nombre,
                descripcion=request.POST.get("descripcion", "").strip(),
                descripcion_larga=request.POST.get("descripcion_larga", "").strip(),
                dimensiones=request.POST.get("dimensiones", "").strip(),
                material=request.POST.get("material", "").strip(),
                capacidad=request.POST.get("capacidad", "").strip(),
                color=request.POST.get("color", "").strip(),
                precio_arriendo=precio,
                unidad_precio=request.POST.get("unidad_precio", "").strip() or "Por unidad",
                imagen=request.POST.get("imagen", "").strip(),
                imagenes=[],
                stock_total=stock_total,
                stock_trabajable=stock_trabajable,
                destacado=request.POST.get("destacado") == "on",
                activo=request.POST.get("activo") == "on",
            )
            if stock_total:
                MovimientoInventario.objects.create(
                    articulo=articulo,
                    usuario=request.usuario_panel,
                    tipo="Ingreso",
                    cantidad=stock_total,
                    stock_anterior=0,
                    stock_nuevo=stock_total,
                    motivo="Stock inicial al crear el producto",
                    fecha=timezone.now(),
                )

        messages.success(request, f"Producto {articulo.nombre} creado con código {articulo.codigo}.")
        return redirect("gestion_inventario")

    inventario_qs = list(
        Articulo.objects
        .filter(negocio=negocio)
        .select_related("categoria")
        .order_by("nombre")
    )

    comprometido = comprometido_vigente([a.id for a in inventario_qs])
    en_uso = _articulos_en_uso([a.id for a in inventario_qs])
    for articulo in inventario_qs:
        articulo.en_uso = articulo.id in en_uso
        articulo.stock_comprometido = comprometido.get(articulo.id, 0)
        articulo.stock_disponible = max(0, articulo.stock_trabajable - articulo.stock_comprometido)

    categorias = (
        Categoria.objects
        .filter(negocio=negocio, activo=True)
        .order_by("nombre")
    )

    return render(
        request,
        "inventario.html",
        {
            "inventario": inventario_qs,
            "categorias": categorias,
        },
    )

@solo_administrador
def gestion_producto(request, id):
    articulo = get_object_or_404(
        Articulo.objects.select_related("categoria", "negocio"),
        id=id,
    )

    if request.method == "POST" and request.POST.get("accion") == "editar":
        nombre = request.POST.get("nombre", "").strip()
        precio = _entero(request.POST.get("precio_arriendo"), -1)
        categoria = Categoria.objects.filter(
            id=request.POST.get("categoria_id"), negocio=articulo.negocio
        ).first()
        stock_total, stock_trabajable, error = _stock_desde_post(
            request.POST, articulo.stock_total, articulo.stock_trabajable
        )

        if not nombre:
            error = "El nombre del producto es obligatorio."
        elif precio < 0:
            error = "El precio de arriendo debe ser un número igual o mayor que cero."
        elif categoria is None:
            error = "Selecciona una categoría válida."

        if error:
            messages.error(request, f"No se guardaron los cambios: {error}")
            return redirect(f"{reverse('gestion_producto', args=[articulo.id])}?editar=1")

        with transaction.atomic():
            articulo = Articulo.objects.select_for_update().get(id=articulo.id)
            anterior_total, anterior_trabajable = articulo.stock_total, articulo.stock_trabajable

            articulo.nombre = nombre
            articulo.categoria = categoria
            articulo.precio_arriendo = precio
            articulo.unidad_precio = request.POST.get("unidad_precio", "").strip() or "Por unidad"
            articulo.descripcion = request.POST.get("descripcion", "").strip()
            articulo.stock_total = stock_total
            articulo.stock_trabajable = stock_trabajable
            articulo.activo = request.POST.get("activo") == "on"
            articulo.save()

            # Cada cambio de stock queda en el historial de movimientos del producto.
            if (stock_total, stock_trabajable) != (anterior_total, anterior_trabajable):
                motivo = request.POST.get("motivo", "").strip() or "Ajuste manual desde el panel"
                MovimientoInventario.objects.create(
                    articulo=articulo,
                    usuario=request.usuario_panel,
                    tipo="Ajuste",
                    cantidad=stock_total - anterior_total,
                    stock_anterior=anterior_total,
                    stock_nuevo=stock_total,
                    motivo=motivo[:255],
                    observaciones=(
                        f"Stock trabajable: {anterior_trabajable} → {stock_trabajable}."
                    ),
                    fecha=timezone.now(),
                )

        comprometido = comprometido_vigente([articulo.id]).get(articulo.id, 0)
        aviso = ""
        if comprometido > stock_trabajable:
            aviso = (
                f" Atención: hay {comprometido} unidades comprometidas en reservas vigentes, "
                "más que el stock trabajable."
            )
        messages.success(
            request,
            f"{articulo.nombre} actualizado: stock total {stock_total}, "
            f"trabajable {stock_trabajable}, disponible {max(0, stock_trabajable - comprometido)}.{aviso}",
        )
        return redirect("gestion_inventario")

    comprometido = comprometido_vigente([articulo.id]).get(articulo.id, 0)

    movimientos = (
        MovimientoInventario.objects
        .filter(articulo=articulo)
        .select_related("usuario")
        .order_by("-fecha")
    )

    return render(
        request,
        "producto-admin.html",
        {
            "articulo": articulo,
            "movimientos": movimientos,
            "categorias": Categoria.objects.filter(negocio=articulo.negocio, activo=True).order_by("nombre"),
            "comprometido": comprometido,
            "disponible": max(0, articulo.stock_trabajable - comprometido),
        },
    )


@solo_administrador
@require_POST
@transaction.atomic
def guardar_producto(request):
    try:
        datos = json.loads(request.body.decode("utf-8"))

        codigo = datos.get("codigo")
        nombre = datos.get("nombre", "").strip()
        categoria_nombre = datos.get("categoria", "").strip()

        stock_total = int(datos.get("stock_total", 0))
        stock_trabajable = int(datos.get("stock_trabajable", 0))
        precio_arriendo = int(datos.get("precio_arriendo", 0))

        if not nombre:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "El nombre es obligatorio.",
                },
                status=400,
            )

        if stock_total < 0:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "El stock total no puede ser negativo.",
                },
                status=400,
            )

        if stock_trabajable < 0:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "El stock trabajable no puede ser negativo.",
                },
                status=400,
            )

        if stock_trabajable > stock_total:
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        "El stock trabajable no puede ser mayor "
                        "que el stock total."
                    ),
                },
                status=400,
            )

        if precio_arriendo < 0:
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        "El precio de arriendo no puede ser negativo."
                    ),
                },
                status=400,
            )

        categoria = get_object_or_404(
            Categoria,
            nombre=categoria_nombre,
        )

        if codigo:
            producto_obj = get_object_or_404(
                Articulo,
                codigo=codigo,
            )

            producto_obj.nombre = nombre
            producto_obj.categoria = categoria
            producto_obj.categoria_label = categoria.nombre
            producto_obj.stock_total = stock_total
            producto_obj.stock_trabajable = stock_trabajable
            producto_obj.precio_arriendo = precio_arriendo
            producto_obj.save()

            return JsonResponse(
                {
                    "ok": True,
                    "modo": "editar",
                    "mensaje": "Producto actualizado correctamente.",
                    "codigo": producto_obj.codigo,
                }
            )

        codigo_nuevo = generar_codigo_producto(categoria)
        negocio = obtener_negocio()

        producto_obj = Articulo.objects.create(
            negocio=negocio,
            codigo=codigo_nuevo,
            nombre=nombre,
            categoria=categoria,
            categoria_label=categoria.nombre,
            stock_total=stock_total,
            stock_trabajable=stock_trabajable,
            precio_arriendo=precio_arriendo,
            descripcion="",
            descripcion_larga="",
            dimensiones="",
            material="",
            capacidad="",
            color="",
            unidad_precio="Por unidad",
            imagen="",
            imagenes=[],
            destacado=False,
            activo=True,
        )

        return JsonResponse(
            {
                "ok": True,
                "modo": "crear",
                "mensaje": "Producto creado correctamente.",
                "codigo": producto_obj.codigo,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {
                "ok": False,
                "error": "Los datos enviados no son válidos.",
            },
            status=400,
        )

    except ValueError:
        return JsonResponse(
            {
                "ok": False,
                "error": "Stock y precio deben ser números válidos.",
            },
            status=400,
        )


# CLIENTES
@solo_administrador
def gestion_clientes(request):
    negocio = obtener_negocio()

    if request.method == "POST" and request.POST.get("accion") == "crear":
        rut = normalizar_rut(request.POST.get("rut", ""))
        if rut and Cliente.objects.filter(negocio=negocio, rut=rut).exists():
            return redirect(f"{reverse('gestion_clientes')}?aviso=rut-duplicado")

        Cliente.objects.create(
            negocio=negocio,
            rut=rut,
            nombre=request.POST.get("nombre", "").strip(),
            apellido=request.POST.get("apellido", "").strip(),
            telefono=request.POST.get("telefono", "").strip(),
            email=request.POST.get("email", "").strip().lower(),
            direccion=request.POST.get("direccion", "").strip(),
            comuna=request.POST.get("comuna", "").strip(),
            observaciones=request.POST.get(
                "observaciones",
                "",
            ).strip(),
            activo=request.POST.get("activo") == "on",
        )

        return redirect("gestion_clientes")

    clientes = list(
        Cliente.objects
        .filter(negocio=negocio)
        .order_by("nombre", "apellido")
    )

    # Añadimos datos auxiliares sin depender de related_name.
    servicios = list(
        Servicio.objects
        .filter(negocio=negocio)
        .only("id", "cliente_id", "fecha_evento")
    )

    conteos = {}
    ultimas_fechas = {}

    for servicio in servicios:
        conteos[servicio.cliente_id] = (
            conteos.get(servicio.cliente_id, 0) + 1
        )

        if servicio.fecha_evento:
            actual = ultimas_fechas.get(servicio.cliente_id)
            if actual is None or servicio.fecha_evento > actual:
                ultimas_fechas[servicio.cliente_id] = servicio.fecha_evento

    for cliente in clientes:
        cliente.servicios_count = conteos.get(cliente.id, 0)
        cliente.ultima_reserva = ultimas_fechas.get(cliente.id)

    return render(
        request,
        "clientes.html",
        {
            "clientes": clientes,
        },
    )

@solo_administrador
def gestion_cliente(request, id):
    cliente = get_object_or_404(
        Cliente.objects.select_related("negocio"),
        id=id,
    )

    if request.method == "POST" and request.POST.get("accion") == "eliminar":
        # Un cliente con cotizaciones o reservas no se elimina, para no perder historial.
        if Servicio.objects.filter(cliente=cliente).exists():
            destino = (
                reverse("gestion_clientes")
                if request.POST.get("origen") == "lista"
                else reverse("gestion_cliente", args=[cliente.id])
            )
            return redirect(f"{destino}?aviso=con-servicios")

        cliente.delete()

        return redirect(
            f"{reverse('gestion_clientes')}?aviso=eliminado"
        )

    if request.method == "POST" and request.POST.get("accion") == "editar":
        rut = normalizar_rut(request.POST.get("rut", ""))
        if rut and Cliente.objects.filter(negocio=cliente.negocio, rut=rut).exclude(id=cliente.id).exists():
            return redirect(f"{reverse('gestion_cliente', args=[cliente.id])}?aviso=rut-duplicado")

        cliente.nombre = request.POST.get("nombre", "").strip()
        cliente.apellido = request.POST.get("apellido", "").strip()
        cliente.rut = rut
        cliente.telefono = request.POST.get("telefono", "").strip()
        cliente.email = request.POST.get("email", "").strip().lower()
        cliente.direccion = request.POST.get("direccion", "").strip()
        cliente.comuna = request.POST.get("comuna", "").strip()
        cliente.observaciones = request.POST.get(
            "observaciones",
            "",
        ).strip()
        cliente.activo = request.POST.get("activo") == "on"
        cliente.save()

        return redirect(
            "gestion_cliente",
            id=cliente.id,
        )

    servicios = (
        Servicio.objects
        .filter(cliente=cliente)
        .order_by("-fecha_creacion")
    )

    return render(
        request,
        "cliente.html",
        {
            "cliente": cliente,
            "servicios": servicios,
        },
    )


# COTIZACIONES / SERVICIO
@solo_administrador
def gestion_cotizaciones(request):
    negocio = obtener_negocio()

    if request.method == "POST" and request.POST.get("accion") == "crear":
        cliente = get_object_or_404(
            Cliente,
            id=request.POST.get("cliente_id"),
            negocio=negocio,
        )

        codigo = f"COT-{date.today():%Y%m%d}-{uuid4().hex[:6].upper()}"

        with transaction.atomic():
            servicio = Servicio.objects.create(
                negocio=negocio,
                cliente=cliente,
                codigo=codigo,
                estado="Cotizacion",
                tipo_servicio=request.POST.get(
                    "tipo_servicio",
                    "",
                ).strip(),
                fecha_solicitud=date.today(),
                fecha_evento=(
                    request.POST.get("fecha_evento") or None
                ),
                fecha_inicio=(
                    request.POST.get("fecha_inicio") or None
                ),
                fecha_termino=(
                    request.POST.get("fecha_termino") or None
                ),
                direccion_evento=request.POST.get(
                    "direccion_evento",
                    "",
                ).strip(),
                comuna_evento=request.POST.get(
                    "comuna_evento",
                    "",
                ).strip(),
                cantidad_personas=(
                    _entero(
                        request.POST.get("cantidad_personas"),
                        0,
                    )
                    or None
                ),
                subtotal=0,
                descuento=_entero(
                    request.POST.get("descuento"),
                    0,
                ),
                total=0,
                abono=_entero(
                    request.POST.get("abono"),
                    0,
                ),
                garantia=_entero(
                    request.POST.get("garantia"),
                    0,
                ),
                observaciones=request.POST.get(
                    "observaciones",
                    "",
                ).strip(),
            )

            articulo_id = request.POST.get("articulo_id")
            cantidad = _entero(
                request.POST.get("cantidad"),
                1,
            )

            if articulo_id:
                articulo = get_object_or_404(
                    Articulo,
                    id=articulo_id,
                )

                subtotal = articulo.precio_arriendo * cantidad

                DetalleServicio.objects.create(
                    servicio=servicio,
                    articulo=articulo,
                    cantidad=cantidad,
                    precio_unitario=articulo.precio_arriendo,
                    subtotal=subtotal,
                )

                servicio.subtotal = subtotal
                servicio.total = max(
                    0,
                    subtotal - servicio.descuento,
                )
                servicio.save()

        return redirect(
            "gestion_cotizacion",
            id=servicio.id,
        )

    # Filtros de la lista: texto (nombre, RUT o correo), estado y rango de fecha del evento.
    filtros = {
        "q": request.GET.get("q", "").strip(),
        "estado": request.GET.get("estado", "").strip(),
        "desde": request.GET.get("desde", "").strip(),
        "hasta": request.GET.get("hasta", "").strip(),
    }

    cotizaciones = (
        Servicio.objects
        .filter(negocio=negocio)
        .select_related("cliente")
        .prefetch_related("categorias_solicitadas")
        .order_by("-fecha_creacion")
    )

    if filtros["q"]:
        texto = filtros["q"]
        rut = normalizar_rut(texto)
        condicion = (
            Q(cliente__nombre__icontains=texto)
            | Q(cliente__apellido__icontains=texto)
            | Q(cliente__email__icontains=texto)
            | Q(cliente__rut__icontains=texto)
        )
        if rut:
            condicion |= Q(cliente__rut__icontains=rut)
        partes = texto.split()
        if len(partes) > 1:
            condicion |= Q(cliente__nombre__icontains=partes[0], cliente__apellido__icontains=" ".join(partes[1:]))
        cotizaciones = cotizaciones.filter(condicion)

    if filtros["estado"] in Servicio.Estado.values:
        cotizaciones = cotizaciones.filter(estado=filtros["estado"])

    for clave, lookup in (("desde", "fecha_evento__gte"), ("hasta", "fecha_evento__lte")):
        try:
            cotizaciones = cotizaciones.filter(**{lookup: date.fromisoformat(filtros[clave])})
        except ValueError:
            filtros[clave] = ""

    clientes = (
        Cliente.objects
        .filter(
            negocio=negocio,
            activo=True,
        )
        .order_by("nombre", "apellido")
    )

    articulos = (
        Articulo.objects
        .filter(
            negocio=negocio,
            activo=True,
        )
        .order_by("nombre")
    )

    return render(
        request,
        "cotizaciones.html",
        {
            "cotizaciones": cotizaciones,
            "clientes": clientes,
            "articulos": articulos,
            "filtros": filtros,
            "estados": Servicio.Estado.choices,
        },
    )

def _recalcular_totales(servicio):
    servicio.subtotal = (
        DetalleServicio.objects
        .filter(servicio=servicio)
        .aggregate(total=Sum("subtotal"))["total"]
        or 0
    )
    servicio.total = max(0, servicio.subtotal - servicio.descuento)
    servicio.save(update_fields=["subtotal", "total", "fecha_actualizacion"])


def _mensaje_faltantes(faltantes):
    detalle = "; ".join(
        f"{f['articulo'].nombre}: solicitado {f['solicitado']}, disponible {f['disponible']}"
        for f in faltantes
    )
    return f"No hay stock suficiente para las fechas del evento. {detalle}."


def _agregar_articulo(request, servicio):
    articulo = get_object_or_404(
        Articulo,
        id=request.POST.get("articulo_id"),
        negocio=servicio.negocio,
        activo=True,
    )
    cantidad = max(1, _entero(request.POST.get("cantidad"), 1))

    with transaction.atomic():
        # Si la reserva ya bloquea stock, la nueva cantidad también debe caber.
        if servicio.estado in ESTADOS_QUE_BLOQUEAN:
            articulo = Articulo.objects.select_for_update().get(id=articulo.id)
            actual = (
                DetalleServicio.objects
                .filter(servicio=servicio, articulo=articulo)
                .aggregate(total=Sum("cantidad"))["total"]
                or 0
            )
            inicio, fin = periodo_servicio(servicio)
            resultado = calcular_disponibilidad(
                articulo, inicio, fin, excluir_servicio=servicio, solicitado=actual + cantidad
            )
            if resultado["faltante"]:
                messages.error(request, _mensaje_faltantes([resultado]))
                return

        detalle = DetalleServicio.objects.filter(servicio=servicio, articulo=articulo).first()
        if detalle:
            detalle.cantidad += cantidad
        else:
            detalle = DetalleServicio(servicio=servicio, articulo=articulo, cantidad=cantidad)
        detalle.precio_unitario = articulo.precio_arriendo
        detalle.subtotal = detalle.cantidad * detalle.precio_unitario
        detalle.save()
        _recalcular_totales(servicio)

    messages.success(request, f"Se agregó {articulo.nombre} a la cotización.")


def _cargar_sugeridos(request, servicio):
    """Agrega artículos sugeridos según tipo de evento, categorías pedidas e invitados."""
    preset = PREDETERMINADOS_EVENTO.get(servicio.tipo_servicio, {})
    if servicio.categorias_por_definir:
        claves = list(preset.keys())
    else:
        claves = [c.nombre for c in servicio.categorias_solicitadas.all()]

    invitados = servicio.cantidad_personas or 0
    existentes = set(
        DetalleServicio.objects.filter(servicio=servicio).values_list("articulo__codigo", flat=True)
    )
    agregados = 0

    with transaction.atomic():
        for clave in claves:
            for regla in preset.get(clave, []):
                if regla["codigo"] in existentes:
                    continue
                articulo = Articulo.objects.filter(
                    negocio=servicio.negocio, codigo=regla["codigo"], activo=True
                ).first()
                cantidad = calcular_cantidad_predeterminada(regla, invitados)
                if articulo is None or cantidad <= 0:
                    continue
                DetalleServicio.objects.create(
                    servicio=servicio,
                    articulo=articulo,
                    cantidad=cantidad,
                    precio_unitario=articulo.precio_arriendo,
                    subtotal=articulo.precio_arriendo * cantidad,
                )
                existentes.add(regla["codigo"])
                agregados += 1
        _recalcular_totales(servicio)

    if agregados:
        messages.success(request, f"Se agregaron {agregados} artículos sugeridos. Revisa y ajusta las cantidades.")
    else:
        messages.error(request, "No hay artículos sugeridos nuevos para este tipo de evento y categorías.")


def _cambiar_estado(request, servicio, nuevo_estado):
    if nuevo_estado not in Servicio.Estado.values or nuevo_estado == servicio.estado:
        return

    # Pasar a un estado que bloquea stock exige comprobar disponibilidad en ese momento.
    if nuevo_estado in ESTADOS_QUE_BLOQUEAN and servicio.estado not in ESTADOS_QUE_BLOQUEAN:
        try:
            confirmar_servicio(servicio.id, nuevo_estado)
        except StockInsuficiente as error:
            messages.error(request, _mensaje_faltantes(error.faltantes))
            return
        except ValueError as error:
            messages.error(request, f"No se puede confirmar: {error}")
            return
        messages.success(request, f"Reserva confirmada. Estado actual: {Servicio.Estado(nuevo_estado).label}.")
        return

    servicio.estado = nuevo_estado
    servicio.save(update_fields=["estado", "fecha_actualizacion"])
    messages.success(request, f"Estado actualizado a {Servicio.Estado(nuevo_estado).label}.")


@solo_administrador
def gestion_cotizacion(request, id):
    servicio = get_object_or_404(
        Servicio.objects.select_related("cliente", "negocio"),
        id=id,
    )

    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "agregar_articulo":
            _agregar_articulo(request, servicio)
        elif accion == "quitar_articulo":
            with transaction.atomic():
                DetalleServicio.objects.filter(servicio=servicio, id=request.POST.get("detalle_id")).delete()
                _recalcular_totales(servicio)
            messages.success(request, "Artículo quitado de la cotización.")
        elif accion == "cargar_sugeridos":
            _cargar_sugeridos(request, servicio)
        elif accion == "confirmar":
            _cambiar_estado(request, servicio, Servicio.Estado.CONFIRMADO)
        elif accion == "cambiar_estado":
            _cambiar_estado(request, servicio, request.POST.get("estado", "").strip())

        return redirect("gestion_cotizacion", id=servicio.id)

    detalles = list(
        DetalleServicio.objects
        .filter(servicio=servicio)
        .select_related("articulo")
        .order_by("articulo__nombre")
    )

    # Disponibilidad por artículo para las fechas del evento (sin contar este mismo servicio).
    por_articulo = {r["articulo_id"]: r for r in disponibilidad_servicio(servicio)}
    for detalle in detalles:
        detalle.disponibilidad = por_articulo.get(detalle.articulo_id)

    categorias = [etiqueta_categoria(c) for c in servicio.categorias_solicitadas.all()]
    hay_faltantes = any(r["faltante"] for r in por_articulo.values())

    return render(
        request,
        "cotizacion.html",
        {
            "servicio": servicio,
            "detalles": detalles,
            "categorias": categorias,
            "articulos": (
                Articulo.objects
                .filter(negocio=servicio.negocio, activo=True)
                .order_by("categoria__nombre", "nombre")
            ),
            "estados_servicio": Servicio.Estado.choices,
            "bloquea_stock": servicio.estado in ESTADOS_QUE_BLOQUEAN,
            "puede_confirmar": (
                servicio.estado in (Servicio.Estado.COTIZACION, Servicio.Estado.CONTACTADO)
                and bool(detalles)
                and servicio.fecha_evento is not None
            ),
            "hay_faltantes": hay_faltantes,
        },
    )


# RESERVAS / SERVICIOS CONFIRMADOS
@solo_administrador
def gestion_reservas(request):
    negocio = obtener_negocio()

    reservas = (
        Servicio.objects
        .filter(negocio=negocio)
        .exclude(
            estado__in=[
                "Cotizacion",
                "Contactado",
                "Cancelado",
            ]
        )
        .select_related("cliente")
        .order_by("-fecha_evento", "-fecha_creacion")
    )

    return render(
        request,
        "reservas.html",
        {
            "reservas": reservas,
        },
    )

@solo_administrador
def gestion_reserva(request, id):
    servicio = get_object_or_404(
        Servicio.objects.select_related("cliente", "negocio"),
        id=id,
    )

    detalles = (
        DetalleServicio.objects
        .filter(servicio=servicio)
        .select_related("articulo")
    )

    return render(
        request,
        "reserva.html",
        {
            "servicio": servicio,
            "detalles": detalles,
        },
    )


# PREPARACIÓN
@solo_administrador
def gestion_preparacion(request):
    preparaciones = (
        Preparacion.objects
        .select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        )
        .order_by("-id")
    )

    return render(
        request,
        "preparacion.html",
        {
            "preparaciones": preparaciones,
        },
    )

@solo_administrador
def gestion_preparacion_documento(request, id):
    preparacion = get_object_or_404(
        Preparacion.objects.select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        ),
        id=id,
    )

    detalles = list(
        DetallePreparacion.objects
        .filter(preparacion=preparacion)
        .select_related("articulo")
    )

    if request.method == "POST":
        with transaction.atomic():
            for detalle in detalles:
                valor = request.POST.get(
                    f"preparado_{detalle.id}"
                )

                if valor is not None:
                    detalle.cantidad_preparada = max(
                        0,
                        _entero(valor, 0),
                    )
                    detalle.save(
                        update_fields=[
                            "cantidad_preparada",
                        ]
                    )

            preparacion.observaciones = request.POST.get(
                "observaciones",
                "",
            ).strip()

            if detalles and all(
                d.cantidad_preparada >= d.cantidad_solicitada
                for d in detalles
            ):
                preparacion.estado = "Completada"

            preparacion.save()

        return redirect(
            "gestion_preparacion_documento",
            id=id,
        )

    return render(
        request,
        "preparacion-documento.html",
        {
            "preparacion": preparacion,
            "detalles": detalles,
        },
    )


# DESPACHOS
@solo_administrador
def gestion_despachos(request):
    despachos = (
        Despacho.objects
        .select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        )
        .order_by("-fecha_despacho")
    )

    return render(
        request,
        "despachos.html",
        {
            "despachos": despachos,
        },
    )

@solo_administrador
def gestion_despacho_documento(request, id):
    despacho = get_object_or_404(
        Despacho.objects.select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        ),
        id=id,
    )

    detalles = (
        DetalleDespacho.objects
        .filter(despacho=despacho)
        .select_related("articulo")
    )

    return render(
        request,
        "despacho-documento.html",
        {
            "despacho": despacho,
            "detalles": detalles,
        },
    )


# DEVOLUCIONES
@solo_administrador
def gestion_devoluciones(request):
    devoluciones = list(
        Devolucion.objects
        .select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        )
        .order_by("-fecha_devolucion")
    )

    # Total por devolución sin depender de related_name.
    ids = [d.id for d in devoluciones]
    totales = {}

    if ids:
        filas = (
            DetalleDevolucion.objects
            .filter(devolucion_id__in=ids)
            .values("devolucion_id")
            .annotate(total=Sum("cantidad_devuelta"))
        )

        for fila in filas:
            totales[fila["devolucion_id"]] = fila["total"] or 0

    for devolucion in devoluciones:
        devolucion.total_productos = totales.get(
            devolucion.id,
            0,
        )

    return render(
        request,
        "devoluciones.html",
        {
            "devoluciones": devoluciones,
        },
    )

@solo_administrador
def gestion_devolucion(request, id):
    devolucion = get_object_or_404(
        Devolucion.objects.select_related(
            "servicio",
            "servicio__cliente",
            "usuario",
        ),
        id=id,
    )

    detalles = list(
        DetalleDevolucion.objects
        .filter(devolucion=devolucion)
        .select_related("articulo")
    )

    if request.method == "POST":
        with transaction.atomic():
            for detalle in detalles:
                detalle.cantidad_devuelta = max(
                    0,
                    _entero(
                        request.POST.get(
                            f"devuelta_{detalle.id}"
                        ),
                        0,
                    ),
                )

                detalle.cantidad_buena = max(
                    0,
                    _entero(
                        request.POST.get(
                            f"buena_{detalle.id}"
                        ),
                        0,
                    ),
                )

                detalle.cantidad_danada = max(
                    0,
                    _entero(
                        request.POST.get(
                            f"danada_{detalle.id}"
                        ),
                        0,
                    ),
                )

                detalle.cantidad_pendiente = max(
                    0,
                    _entero(
                        request.POST.get(
                            f"pendiente_{detalle.id}"
                        ),
                        0,
                    ),
                )

                detalle.save()

            devolucion.observaciones = request.POST.get(
                "observaciones",
                "",
            ).strip()
            devolucion.save()

        return redirect(
            "gestion_devolucion",
            id=id,
        )

    return render(
        request,
        "devolucion.html",
        {
            "devolucion": devolucion,
            "detalles": detalles,
        },
    )


# MERMAS
@solo_administrador
def gestion_mermas(request):
    negocio = obtener_negocio()

    if request.method == "POST" and request.POST.get("accion") == "crear":
        articulo = get_object_or_404(
            Articulo,
            id=request.POST.get("articulo_id"),
        )

        detalle = None
        detalle_id = request.POST.get(
            "detalle_devolucion_id"
        )

        if detalle_id:
            detalle = get_object_or_404(
                DetalleDevolucion,
                id=detalle_id,
            )

        fecha_valor = request.POST.get("fecha")

        Merma.objects.create(
            detalle_devolucion=detalle,
            articulo=articulo,
            cantidad=max(
                1,
                _entero(
                    request.POST.get("cantidad"),
                    1,
                ),
            ),
            tipo=request.POST.get("tipo", "").strip(),
            motivo=request.POST.get("motivo", "").strip(),
            valor_reposicion=max(
                0,
                _entero(
                    request.POST.get("valor_reposicion"),
                    0,
                ),
            ),
            observaciones=request.POST.get(
                "observaciones",
                "",
            ).strip(),
            fecha=fecha_valor or datetime.now(),
        )

        return redirect("gestion_mermas")

    mermas = (
        Merma.objects
        .select_related(
            "articulo",
            "articulo__categoria",
            "detalle_devolucion",
            "detalle_devolucion__devolucion",
            "detalle_devolucion__devolucion__servicio",
            "detalle_devolucion__devolucion__usuario",
        )
        .order_by("-fecha")
    )

    articulos = (
        Articulo.objects
        .filter(
            negocio=negocio,
            activo=True,
        )
        .order_by("nombre")
    )

    detalles_devolucion = (
        DetalleDevolucion.objects
        .select_related(
            "devolucion",
            "devolucion__servicio",
            "articulo",
        )
        .order_by("-id")
    )

    return render(
        request,
        "mermas.html",
        {
            "mermas": mermas,
            "articulos": articulos,
            "detalles_devolucion": detalles_devolucion,
        },
    )


# CALENDARIO
@solo_administrador
def gestion_calendario(request):
    negocio = obtener_negocio()

    servicios = (
        Servicio.objects
        .filter(negocio=negocio)
        .filter(Q(fecha_evento__isnull=False) | Q(fecha_inicio__isnull=False))
        .exclude(estado=Servicio.Estado.CANCELADO)
        .select_related("cliente")
    )

    def url_servicio(servicio):
        if servicio.estado in (Servicio.Estado.COTIZACION, Servicio.Estado.CONTACTADO):
            return reverse("gestion_cotizacion", args=[servicio.id])
        return reverse("gestion_reserva", args=[servicio.id])

    def cliente_de(servicio):
        return f"{servicio.cliente.nombre} {servicio.cliente.apellido}".strip()

    eventos = []
    for servicio in servicios:
        inicio = servicio.fecha_inicio or servicio.fecha_evento
        fin = servicio.fecha_termino or inicio
        eventos.append({
            "tipo": "evento",
            "titulo": f"{servicio.tipo_servicio or 'Evento'} · {cliente_de(servicio)}",
            "inicio": inicio.isoformat(),
            "fin": max(fin, inicio).isoformat(),
            "hora_inicio": servicio.hora_inicio.strftime("%H:%M") if servicio.hora_inicio else None,
            "hora_fin": servicio.hora_termino.strftime("%H:%M") if servicio.hora_termino else None,
            "codigo": servicio.codigo,
            "cliente": cliente_de(servicio),
            "estado": servicio.get_estado_display(),
            # Las cotizaciones aún no están confirmadas: se dibujan como tentativas.
            "tentativo": servicio.estado in (Servicio.Estado.COTIZACION, Servicio.Estado.CONTACTADO),
            "lugar": ", ".join(filter(None, [servicio.direccion_evento, servicio.comuna_evento])),
            "personas": servicio.cantidad_personas,
            "url": url_servicio(servicio),
        })

    movimientos = [
        ("entrega", "Entrega", d.servicio, d.fecha_despacho)
        for d in Despacho.objects.filter(servicio__negocio=negocio).select_related("servicio__cliente")
    ] + [
        ("retiro", "Retiro", d.servicio, d.fecha_devolucion)
        for d in Devolucion.objects.filter(servicio__negocio=negocio).select_related("servicio__cliente")
    ]
    for tipo, etiqueta, servicio, momento in movimientos:
        momento = timezone.localtime(momento) if timezone.is_aware(momento) else momento
        eventos.append({
            "tipo": tipo,
            "titulo": f"{etiqueta} · {cliente_de(servicio)}",
            "inicio": momento.date().isoformat(),
            "fin": momento.date().isoformat(),
            "hora_inicio": momento.strftime("%H:%M"),
            "hora_fin": None,
            "codigo": servicio.codigo,
            "cliente": cliente_de(servicio),
            "estado": servicio.get_estado_display(),
            "tentativo": False,
            "lugar": ", ".join(filter(None, [servicio.direccion_evento, servicio.comuna_evento])),
            "personas": servicio.cantidad_personas,
            "url": url_servicio(servicio),
        })

    return render(
        request,
        "calendario.html",
        {
            "eventos": eventos,
        },
    )


# REPORTES
@solo_administrador
def gestion_reportes(request):
    negocio = obtener_negocio()

    articulos = Articulo.objects.filter(negocio=negocio)
    servicios = Servicio.objects.filter(negocio=negocio)
    clientes = Cliente.objects.filter(negocio=negocio)

    stock_total = (
        articulos.aggregate(total=Sum("stock_total"))["total"]
        or 0
    )

    stock_trabajable = (
        articulos.aggregate(total=Sum("stock_trabajable"))["total"]
        or 0
    )

    monto_cotizado = (
        servicios
        .filter(estado="Cotizacion")
        .aggregate(total=Sum("total"))["total"]
        or 0
    )

    ticket_promedio = (
        servicios
        .filter(estado="Finalizado")
        .aggregate(promedio=Avg("total"))["promedio"]
        or 0
    )

    comprometido = comprometido_vigente(list(articulos.values_list("id", flat=True)))

    metricas = {
        "stock_total": stock_total,
        "stock_trabajable": stock_trabajable,
        "stock_comprometido": sum(comprometido.values()),
        "stock_disponible": sum(
            max(0, a.stock_trabajable - comprometido.get(a.id, 0)) for a in articulos
        ),
        "stock_bajo": articulos.filter(
            stock_trabajable__lte=10
        ).count(),
        "reservas_total": servicios.exclude(
            estado__in=["Cotizacion", "Contactado"]
        ).count(),
        "reservas_proximas": (
            servicios
            .filter(fecha_evento__gte=date.today())
            .exclude(
                estado__in=[
                    "Cotizacion",
                    "Contactado",
                    "Cancelado",
                    "Finalizado",
                ]
            )
            .count()
        ),
        "reservas_finalizadas": servicios.filter(
            estado="Finalizado"
        ).count(),
        "reservas_canceladas": servicios.filter(
            estado="Cancelado"
        ).count(),
        "clientes_total": clientes.count(),
        "clientes_nuevos": clientes.filter(
            fecha_creacion__year=date.today().year,
            fecha_creacion__month=date.today().month,
        ).count(),
        "monto_cotizado": monto_cotizado,
        "cotizaciones_aceptadas": servicios.filter(
            fecha_confirmacion__isnull=False
        ).count(),
        "cotizaciones_rechazadas": servicios.filter(
            estado="Cancelado"
        ).count(),
        "ticket_promedio": ticket_promedio,
    }

    productos_mas_arrendados = list(
        DetalleServicio.objects
        .filter(
            servicio__negocio=negocio,
            servicio__estado__in=ESTADOS_RESERVA,
        )
        .values(
            nombre=F("articulo__nombre")
        )
        .annotate(
            total_arrendado=Sum("cantidad")
        )
        .order_by("-total_arrendado")[:5]
    )

    productos_menos_utilizados = list(
        DetalleServicio.objects
        .filter(
            servicio__negocio=negocio,
            servicio__estado__in=ESTADOS_RESERVA,
        )
        .values(
            nombre=F("articulo__nombre")
        )
        .annotate(
            total_arrendado=Sum("cantidad")
        )
        .order_by("total_arrendado")[:5]
    )

    clientes_frecuentes = list(
        Cliente.objects
        .filter(negocio=negocio)
        .values(
            "id",
            "nombre",
            "apellido",
        )
        .annotate(
            total_servicios=Count(
                "servicios",
                distinct=True,
            )
        )
        .order_by("-total_servicios")[:5]
    )

    ingresos_categoria = list(
        DetalleServicio.objects
        .filter(
            servicio__negocio=negocio,
            servicio__estado__in=ESTADOS_RESERVA,
        )
        .values(
            categoria=F(
                "articulo__categoria__nombre"
            )
        )
        .annotate(
            total=Sum("subtotal")
        )
        .order_by("-total")
    )

    return render(
        request,
        "reportes.html",
        {
            "metricas": metricas,
            "productos_mas_arrendados":
                productos_mas_arrendados,
            "productos_menos_utilizados":
                productos_menos_utilizados,
            "clientes_frecuentes":
                clientes_frecuentes,
            "ingresos_categoria":
                ingresos_categoria,
        },
    )


# USUARIOS
@solo_administrador
def gestion_usuarios(request):
    negocio = obtener_negocio()

    if request.method == "POST" and request.POST.get("accion") == "crear":
        rol = get_object_or_404(
            Rol,
            id=request.POST.get("rol_id"),
        )

        Usuario.objects.create(
            negocio=negocio,
            rol=rol,
            nombre=request.POST.get("nombre", "").strip(),
            apellido=request.POST.get("apellido", "").strip(),
            email=request.POST.get("email", "").strip().lower(),
            password=make_password(
                request.POST.get("password", "")
            ),
            activo=request.POST.get("activo") == "on",
        )

        return redirect("gestion_usuarios")

    usuarios = (
        Usuario.objects
        .filter(negocio=negocio)
        .select_related("rol")
        .order_by("nombre", "apellido")
    )

    roles = Rol.objects.all().order_by("nombre")

    return render(
        request,
        "usuarios.html",
        {
            "usuarios": usuarios,
            "roles": roles,
        },
    )


# CONFIGURACIÓN
@solo_administrador
def gestion_configuracion(request):
    negocio = obtener_negocio()

    if negocio is None:
        return render(
            request,
            "configuracion.html",
            {
                "negocio": None,
                "error": "No existe un negocio activo configurado.",
            },
        )

    if request.method == "POST":
        negocio.nombre = request.POST.get(
            "nombre",
            "",
        ).strip()

        negocio.nombre_fantasia = request.POST.get(
            "nombre_fantasia",
            "",
        ).strip()

        negocio.rut = request.POST.get(
            "rut",
            "",
        ).strip()

        negocio.telefono = request.POST.get(
            "telefono",
            "",
        ).strip()

        negocio.email = request.POST.get(
            "email",
            "",
        ).strip()

        negocio.direccion = request.POST.get(
            "direccion",
            "",
        ).strip()

        negocio.descripcion = request.POST.get(
            "descripcion",
            "",
        ).strip()

        negocio.porcentaje_abono = (
            request.POST.get("porcentaje_abono")
            or 0
        )

        negocio.porcentaje_garantia = (
            request.POST.get("porcentaje_garantia")
            or 0
        )

        negocio.activo = (
            request.POST.get("activo") == "on"
        )

        negocio.save()

        return redirect("gestion_configuracion")

    return render(
        request,
        "configuracion.html",
        {
            "negocio": negocio,
        },
    )


@solo_administrador
def enviar_cotizacion_email(request, id):

    servicio = get_object_or_404(
        Servicio.objects.select_related(
            "cliente",
            "negocio"
        ),
        id=id,
    )

    cliente = servicio.cliente

    if not cliente.email:
        return redirect(
            "gestion_cotizacion",
            id=servicio.id,
        )

    detalles = (
        DetalleServicio.objects
        .filter(servicio=servicio)
        .select_related("articulo")
        .order_by("articulo__nombre")
    )

    subject = (
        f"Cotización {servicio.codigo} "
        f"· Entre Platos y Copas"
    )

    from_email = settings.DEFAULT_FROM_EMAIL

    to = [
        cliente.email
    ]

    text_content = f"""
Hola {cliente.nombre},

Te enviamos la cotización {servicio.codigo}
para tu evento.

Fecha del evento: {servicio.fecha_evento}
Total: ${servicio.total}

Si deseas realizar algún cambio o confirmar la cotización,
puedes responder este correo o comunicarte con nosotros.

Entre Platos y Copas
"""

    filas_articulos = ""

    for detalle in detalles:
        filas_articulos += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #ddd;">
                {detalle.articulo.nombre}
            </td>

            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:center;">
                {detalle.cantidad}
            </td>

            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:right;">
                ${detalle.precio_unitario:,.0f}
            </td>

            <td style="padding:8px;border-bottom:1px solid #ddd;text-align:right;">
                ${detalle.subtotal:,.0f}
            </td>
        </tr>
        """

    html_content = f"""
    <div style="
        font-family:Arial,sans-serif;
        max-width:700px;
        margin:auto;
        color:#333;
    ">

        <h2 style="color:#b98b52;">
            Entre Platos y Copas
        </h2>

        <p>
            Hola <strong>{cliente.nombre}</strong>,
        </p>

        <p>
            Te enviamos la cotización
            <strong>{servicio.codigo}</strong>
            correspondiente a tu evento.
        </p>

        <table
            style="
                width:100%;
                border-collapse:collapse;
                margin-top:20px;
            "
        >

            <thead>
                <tr style="background:#f3ede4;">
                    <th style="padding:8px;text-align:left;">
                        Artículo
                    </th>

                    <th style="padding:8px;">
                        Cantidad
                    </th>

                    <th style="padding:8px;text-align:right;">
                        Precio
                    </th>

                    <th style="padding:8px;text-align:right;">
                        Subtotal
                    </th>
                </tr>
            </thead>

            <tbody>
                {filas_articulos}
            </tbody>

        </table>

        <div style="
            margin-top:20px;
            text-align:right;
        ">

            <p>
                Subtotal:
                <strong>
                    ${servicio.subtotal:,.0f}
                </strong>
            </p>

            <p>
                Descuento:
                <strong>
                    ${servicio.descuento:,.0f}
                </strong>
            </p>

            <p style="font-size:20px;">
                Total:
                <strong>
                    ${servicio.total:,.0f}
                </strong>
            </p>

        </div>

        <hr style="
            border:none;
            border-top:1px solid #ddd;
            margin:30px 0;
        ">

        <p>
            <strong>Fecha del evento:</strong>
            {servicio.fecha_evento or "Por confirmar"}
        </p>

        <p>
            <strong>Comuna:</strong>
            {servicio.comuna_evento or "Por confirmar"}
        </p>

        <p>
            Si deseas modificar o confirmar esta cotización,
            puedes responder este correo o comunicarte con nosotros.
        </p>

        <p>
            Saludos,<br>
            <strong>Entre Platos y Copas</strong>
        </p>

    </div>
    """

    msg = EmailMultiAlternatives(
        subject,
        text_content,
        from_email,
        to,
    )

    msg.attach_alternative(
        html_content,
        "text/html",
    )

    msg.send()

    return redirect(
        "gestion_cotizacion",
        id=servicio.id,
    )


import math


PREDETERMINADOS_EVENTO = {

    "Matrimonio": {

        "vajilla": [
            {
                "codigo": "vaj-003",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cristaleria": [
            {
                "codigo": "cris-001",
                "regla": "por_persona",
                "factor": 1.05,
            },
            {
                "codigo": "cris-003",
                "regla": "por_persona",
                "factor": 1.05,
            },
        ],

        "cubiertos": [
            {
                "codigo": "cub-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "mesas-sillas": [
            {
                "codigo": "sil-001",
                "regla": "por_persona",
                "factor": 1,
            },
            {
                "codigo": "mes-001",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
        ],

        "manteleria": [
            {
                "codigo": "man-001",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
            {
                "codigo": "man-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],
    },


    "Cumpleaños": {

        "vajilla": [
            {
                "codigo": "vaj-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cubiertos": [
            {
                "codigo": "cub-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "mesas-sillas": [
            {
                "codigo": "sil-001",
                "regla": "por_persona",
                "factor": 1,
            },
            {
                "codigo": "mes-001",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
        ],

        "cristaleria": [
            {
                "codigo": "cris-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],
    },


    "Evento empresarial": {

        "vajilla": [
            {
                "codigo": "vaj-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cubiertos": [
            {
                "codigo": "cub-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cristaleria": [
            {
                "codigo": "cris-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "mesas-sillas": [
            {
                "codigo": "mes-002",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
        ],

        "equipamiento": [
            {
                "codigo": "equ-003",
                "regla": "fijo",
                "cantidad": 1,
            },
        ],
    },


    "Cena / Banquete": {

        "vajilla": [
            {
                "codigo": "vaj-003",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cubiertos": [
            {
                "codigo": "cub-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cristaleria": [
            {
                "codigo": "cris-001",
                "regla": "por_persona",
                "factor": 1,
            },
            {
                "codigo": "cris-002",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "mesas-sillas": [
            {
                "codigo": "mes-002",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
        ],

        "manteleria": [
            {
                "codigo": "man-002",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
            {
                "codigo": "man-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],
    },


    "Evento familiar": {

        "vajilla": [
            {
                "codigo": "vaj-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "cubiertos": [
            {
                "codigo": "cub-001",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "mesas-sillas": [
            {
                "codigo": "sil-001",
                "regla": "por_persona",
                "factor": 1,
            },
            {
                "codigo": "mes-001",
                "regla": "por_capacidad",
                "capacidad": 10,
            },
        ],
    },


    "Celebración": {

        "cristaleria": [
            {
                "codigo": "cris-003",
                "regla": "por_persona",
                "factor": 1,
            },
            {
                "codigo": "cris-004",
                "regla": "por_persona",
                "factor": 1,
            },
        ],

        "equipamiento": [
            {
                "codigo": "equ-002",
                "regla": "fijo",
                "cantidad": 1,
            },
        ],
    },


    "Otro": {},
}

def calcular_cantidad_predeterminada(regla, invitados):

    if regla["regla"] == "por_persona":
        return math.ceil(
            invitados
            * regla.get("factor", 1)
        )

    if regla["regla"] == "por_capacidad":
        return math.ceil(
            invitados
            / regla.get("capacidad", 1)
        )

    if regla["regla"] == "fijo":
        return regla.get(
            "cantidad",
            1
        )

    return 0

