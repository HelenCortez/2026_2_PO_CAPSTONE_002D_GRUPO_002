from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from Inventario.models import Articulo, DetalleServicio, Servicio


# Solo estos estados retienen stock: una cotización o un servicio cancelado o devuelto no lo bloquea.
ESTADOS_QUE_BLOQUEAN = [
    Servicio.Estado.CONFIRMADO,
    Servicio.Estado.PREPARACION,
    Servicio.Estado.DESPACHADO,
    Servicio.Estado.DEVOLUCION_PARCIAL,
]


class StockInsuficiente(Exception):
    """Se lanza al confirmar cuando algún artículo no alcanza; trae el detalle de los faltantes."""

    def __init__(self, faltantes):
        super().__init__("Stock insuficiente")
        self.faltantes = faltantes


def periodo_servicio(servicio):
    """Rango de fechas que ocupa un servicio: inicio/término o, si faltan, la fecha del evento."""
    inicio = servicio.fecha_inicio or servicio.fecha_evento
    fin = servicio.fecha_termino or servicio.fecha_evento or inicio
    return inicio, fin


def cantidad_comprometida(articulo, fecha_inicio, fecha_fin, excluir_servicio=None):
    """Unidades del artículo retenidas por servicios activos cuyo periodo se cruza con el solicitado."""
    detalles = (
        DetalleServicio.objects
        .filter(articulo=articulo, servicio__estado__in=ESTADOS_QUE_BLOQUEAN)
        .annotate(
            ini=Coalesce("servicio__fecha_inicio", "servicio__fecha_evento"),
            fin=Coalesce("servicio__fecha_termino", "servicio__fecha_evento", "servicio__fecha_inicio"),
        )
        .filter(ini__lte=fecha_fin, fin__gte=fecha_inicio)
    )
    if excluir_servicio is not None:
        detalles = detalles.exclude(servicio=excluir_servicio)
    return detalles.aggregate(total=Sum("cantidad"))["total"] or 0


def calcular_disponibilidad(articulo, fecha_inicio, fecha_fin, excluir_servicio=None, solicitado=0):
    comprometido = cantidad_comprometida(articulo, fecha_inicio, fecha_fin, excluir_servicio)
    disponible = max(0, articulo.stock_trabajable - comprometido)
    return {
        "articulo_id": articulo.id,
        "articulo": articulo,
        "stock_trabajable": articulo.stock_trabajable,
        "comprometido": comprometido,
        "disponible": disponible,
        "solicitado": solicitado,
        "faltante": max(0, solicitado - disponible),
    }


def disponibilidad_servicio(servicio):
    """Disponibilidad de cada artículo del servicio, sin contar al propio servicio."""
    inicio, fin = periodo_servicio(servicio)
    solicitados = (
        DetalleServicio.objects
        .filter(servicio=servicio)
        .values("articulo_id")
        .annotate(total=Sum("cantidad"))
    )
    cantidades = {fila["articulo_id"]: fila["total"] for fila in solicitados}
    articulos = Articulo.objects.filter(id__in=cantidades).order_by("nombre")

    if inicio is None:
        return [
            {"articulo": a, "articulo_id": a.id, "solicitado": cantidades[a.id],
             "disponible": None, "faltante": 0, "stock_trabajable": a.stock_trabajable, "comprometido": 0}
            for a in articulos
        ]
    return [
        calcular_disponibilidad(a, inicio, fin, excluir_servicio=servicio, solicitado=cantidades[a.id])
        for a in articulos
    ]


def confirmar_servicio(servicio_id, nuevo_estado=Servicio.Estado.CONFIRMADO):
    """
    Pasa un servicio a un estado que bloquea stock solo si hay disponibilidad.
    Bloquea las filas de los artículos para que dos confirmaciones simultáneas no sobrevendan.
    """
    with transaction.atomic():
        servicio = Servicio.objects.select_for_update().get(id=servicio_id)
        inicio, _ = periodo_servicio(servicio)
        if inicio is None:
            raise ValueError("El servicio no tiene fecha de evento.")

        articulo_ids = list(
            DetalleServicio.objects.filter(servicio=servicio).values_list("articulo_id", flat=True)
        )
        if not articulo_ids:
            raise ValueError("El servicio no tiene artículos.")

        # Mismo orden siempre (por id) para evitar bloqueos cruzados entre transacciones.
        list(Articulo.objects.select_for_update().filter(id__in=articulo_ids).order_by("id"))

        faltantes = [r for r in disponibilidad_servicio(servicio) if r["faltante"] > 0]
        if faltantes:
            raise StockInsuficiente(faltantes)

        servicio.estado = nuevo_estado
        if not servicio.fecha_confirmacion:
            servicio.fecha_confirmacion = timezone.now()
        servicio.save(update_fields=["estado", "fecha_confirmacion", "fecha_actualizacion"])
        return servicio
