import threading
import unittest
from datetime import date, timedelta

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from Inventario.disponibilidad import (
    StockInsuficiente,
    calcular_disponibilidad,
    confirmar_servicio,
    disponibilidad_servicio,
)
from Inventario.models import (
    Articulo,
    Categoria,
    Cliente,
    DetalleServicio,
    Negocio,
    Rol,
    Servicio,
    Usuario,
)
from Inventario.validaciones import normalizar_rut, rut_valido, telefono_valido

EN_10_DIAS = date.today() + timedelta(days=10)
RUT_VALIDO = "12.345.678-5"


def crear_base():
    """Negocio, dos categorías y un artículo con 100 unidades trabajables."""
    negocio = Negocio.objects.create(nombre="Entre Platos y Copas")
    vajilla = Categoria.objects.create(negocio=negocio, nombre="vajilla")
    cristaleria = Categoria.objects.create(negocio=negocio, nombre="cristaleria")
    plato = Articulo.objects.create(
        negocio=negocio, categoria=vajilla, codigo="vaj-001", nombre="Plato principal",
        precio_arriendo=500, stock_total=100, stock_trabajable=100,
    )
    return negocio, vajilla, cristaleria, plato


def crear_servicio(negocio, articulo, cantidad, inicio, fin=None, estado=Servicio.Estado.CONFIRMADO, rut="111111111"):
    cliente, _ = Cliente.objects.get_or_create(negocio=negocio, rut=rut, defaults={"nombre": "Cliente"})
    servicio = Servicio.objects.create(
        negocio=negocio, cliente=cliente, codigo=f"COT-{Servicio.objects.count() + 1}",
        estado=estado, fecha_solicitud=date.today(), fecha_evento=inicio,
        fecha_inicio=inicio, fecha_termino=fin or inicio,
    )
    DetalleServicio.objects.create(
        servicio=servicio, articulo=articulo, cantidad=cantidad,
        precio_unitario=articulo.precio_arriendo, subtotal=articulo.precio_arriendo * cantidad,
    )
    return servicio


class ValidacionesTests(TestCase):
    def test_normaliza_rut(self):
        self.assertEqual(normalizar_rut("12.345.678-k"), "12345678K")
        self.assertEqual(normalizar_rut(" 12345678-5 "), "123456785")

    def test_digito_verificador(self):
        self.assertTrue(rut_valido("123456785"))
        self.assertFalse(rut_valido("123456789"))
        self.assertFalse(rut_valido("12A"))

    def test_telefono(self):
        self.assertTrue(telefono_valido("+56 9 4205 8820"))
        self.assertFalse(telefono_valido("123"))
        self.assertFalse(telefono_valido("hola"))


class CotizadorPublicoTests(TestCase):
    def setUp(self):
        self.negocio, self.vajilla, self.cristaleria, self.plato = crear_base()
        self.url = reverse("cotizador")

    def datos(self, **cambios):
        datos = {
            "tipoEvento": "Matrimonio",
            "fecha": EN_10_DIAS.isoformat(),
            "invitados": "80",
            "comuna": "Maipú",
            "despacho": "Sí",
            "categorias": [str(self.vajilla.id), str(self.cristaleria.id)],
            "nombre": "Ana María Pérez",
            "rut": RUT_VALIDO,
            "telefono": "+56 9 1234 5678",
            "correo": "ana@ejemplo.cl",
            "medioContacto": "WhatsApp",
            "comentarios": "Matrimonio al aire libre.",
        }
        datos.update(cambios)
        return {k: v for k, v in datos.items() if v is not None}

    def enviar(self, **cambios):
        return self.client.post(self.url, self.datos(**cambios), HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_solicitud_valida_crea_cliente_y_servicio(self):
        respuesta = self.enviar()
        self.assertEqual(respuesta.status_code, 200)
        cuerpo = respuesta.json()
        self.assertTrue(cuerpo["ok"])
        self.assertEqual(set(cuerpo), {"ok", "message"})

        cliente = Cliente.objects.get()
        self.assertEqual(cliente.rut, "123456785")
        self.assertEqual((cliente.nombre, cliente.apellido), ("Ana", "María Pérez"))

        servicio = Servicio.objects.get()
        self.assertEqual(servicio.estado, Servicio.Estado.COTIZACION)
        self.assertEqual(servicio.cantidad_personas, 80)
        self.assertTrue(servicio.requiere_despacho)
        self.assertEqual(servicio.medio_contacto, "WhatsApp")
        self.assertEqual(servicio.comentarios, "Matrimonio al aire libre.")
        self.assertEqual(set(servicio.categorias_solicitadas.all()), {self.vajilla, self.cristaleria})
        # Solo se guarda lo que pidió el cliente: sin artículos ni cantidades inventadas.
        self.assertFalse(DetalleServicio.objects.exists())

    def test_respuesta_no_expone_codigo(self):
        respuesta = self.enviar()
        servicio = Servicio.objects.get()
        self.assertNotIn(servicio.codigo, respuesta.content.decode())
        self.assertNotIn(str(servicio.id), respuesta.json()["message"])

    def test_reutiliza_cliente_con_otro_formato_de_rut(self):
        self.enviar()
        self.enviar(rut="12345678-5", telefono="", correo="", comentarios="")
        self.assertEqual(Cliente.objects.count(), 1)
        self.assertEqual(Servicio.objects.count(), 2)
        cliente = Cliente.objects.get()
        # Los datos opcionales que vienen vacíos no borran los anteriores.
        self.assertEqual(cliente.email, "ana@ejemplo.cl")
        self.assertEqual(cliente.telefono, "+56 9 1234 5678")

    def test_reconoce_cliente_antiguo_con_rut_con_puntos(self):
        Cliente.objects.create(negocio=self.negocio, rut="12.345.678-5", nombre="Ana")
        self.enviar()
        self.assertEqual(Cliente.objects.count(), 1)
        self.assertEqual(Cliente.objects.get().rut, "123456785")

    def test_rut_registrado_con_otro_nombre_se_rechaza(self):
        self.enviar()
        respuesta = self.enviar(nombre="Juan Soto")
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("rut", respuesta.json()["errors"])
        # No expone el nombre registrado.
        self.assertNotIn("Ana", respuesta.content.decode())
        self.assertEqual(Servicio.objects.count(), 1)

    def test_rut_registrado_acepta_variantes_del_mismo_nombre(self):
        self.enviar()
        for nombre in ("ana perez", "Ana María Pérez Soto", "ANA MARIA PEREZ"):
            self.assertTrue(self.enviar(nombre=nombre).json()["ok"], nombre)
        self.assertEqual(Cliente.objects.count(), 1)

    def test_campos_opcionales_vacios(self):
        respuesta = self.enviar(telefono="", correo="", medioContacto="", comentarios="", direccion="")
        self.assertTrue(respuesta.json()["ok"])

    def test_aun_no_estoy_seguro_es_excluyente(self):
        self.enviar(categorias=["por-definir", str(self.vajilla.id)])
        servicio = Servicio.objects.get()
        self.assertTrue(servicio.categorias_por_definir)
        self.assertFalse(servicio.categorias_solicitadas.exists())

    def test_errores_de_validacion(self):
        casos = {
            "rut": {"rut": "12.345.678-9"},
            "fecha": {"fecha": (date.today() - timedelta(days=1)).isoformat()},
            "invitados": {"invitados": "0"},
            "categorias": {"categorias": []},
            "correo": {"correo": "no-es-correo"},
            "telefono": {"telefono": "12"},
            "tipoEvento": {"tipoEvento": "Fiesta inventada"},
            "nombre": {"nombre": ""},
        }
        for campo, cambios in casos.items():
            with self.subTest(campo=campo):
                respuesta = self.enviar(**cambios)
                self.assertEqual(respuesta.status_code, 400)
                self.assertIn(campo, respuesta.json()["errors"])
        self.assertFalse(Servicio.objects.exists())
        self.assertFalse(Cliente.objects.exists())

    def test_categoria_desconocida(self):
        respuesta = self.enviar(categorias=["99999"])
        self.assertIn("categorias", respuesta.json()["errors"])

    def test_envio_sin_javascript_muestra_mensaje(self):
        respuesta = self.client.post(self.url, self.datos())
        self.assertContains(respuesta, "fue enviada correctamente")
        self.assertNotContains(respuesta, Servicio.objects.get().codigo)


class AdministracionTests(TestCase):
    def setUp(self):
        self.negocio, self.vajilla, _, self.plato = crear_base()
        rol = Rol.objects.create(nombre="Administrador")
        self.admin = Usuario.objects.create(
            negocio=self.negocio, rol=rol, nombre="Helen", apellido="Cortés",
            email="admin@ejemplo.cl", password="x",
        )
        self.client.post(reverse("cotizador"), {
            "tipoEvento": "Cumpleaños", "fecha": EN_10_DIAS.isoformat(), "invitados": "40",
            "comuna": "Maipú", "despacho": "No, retiro yo", "categorias": [str(self.vajilla.id)],
            "nombre": "Pedro Soto", "rut": RUT_VALIDO, "correo": "pedro@ejemplo.cl",
            "medioContacto": "Correo", "comentarios": "Necesito platos negros.",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.servicio = Servicio.objects.get()

    def iniciar_sesion(self, usuario=None):
        sesion = self.client.session
        sesion["usuario_id"] = (usuario or self.admin).id
        sesion.save()

    def test_sin_sesion_redirige_al_login(self):
        for url in (reverse("gestion_cotizaciones"), reverse("gestion_cotizacion", args=[self.servicio.id])):
            respuesta = self.client.get(url)
            self.assertEqual(respuesta.status_code, 302)
            self.assertIn(reverse("gestion_login"), respuesta["Location"])

    def test_rol_sin_permiso_recibe_403(self):
        otro_rol = Rol.objects.create(nombre="Bodega")
        usuario = Usuario.objects.create(
            negocio=self.negocio, rol=otro_rol, nombre="B", apellido="B", email="b@ejemplo.cl", password="x",
        )
        self.iniciar_sesion(usuario)
        self.assertEqual(self.client.get(reverse("gestion_cotizaciones")).status_code, 403)

    def test_lista_muestra_y_filtra_la_solicitud(self):
        self.iniciar_sesion()
        url = reverse("gestion_cotizaciones")
        fila = reverse("gestion_cotizacion", args=[self.servicio.id])
        self.assertContains(self.client.get(url), fila)
        self.assertContains(self.client.get(url, {"q": "12.345.678-5"}), fila)
        self.assertContains(self.client.get(url, {"q": "pedro@ejemplo"}), fila)
        self.assertContains(self.client.get(url, {"q": "Pedro Soto"}), fila)
        self.assertContains(self.client.get(url, {"estado": "Cotizacion"}), fila)
        self.assertNotContains(self.client.get(url, {"estado": "Confirmado"}), fila)
        self.assertNotContains(self.client.get(url, {"desde": (EN_10_DIAS + timedelta(days=1)).isoformat()}), fila)

    def test_detalle_muestra_categorias_y_comentarios(self):
        self.iniciar_sesion()
        respuesta = self.client.get(reverse("gestion_cotizacion", args=[self.servicio.id]))
        self.assertContains(respuesta, "Vajilla")
        self.assertContains(respuesta, "Necesito platos negros.")
        self.assertContains(respuesta, "123456785")

    def test_confirmar_desde_el_detalle(self):
        self.iniciar_sesion()
        url = reverse("gestion_cotizacion", args=[self.servicio.id])
        self.client.post(url, {"accion": "agregar_articulo", "articulo_id": self.plato.id, "cantidad": 40})
        self.client.post(url, {"accion": "confirmar"})
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.estado, Servicio.Estado.CONFIRMADO)
        self.assertIsNotNone(self.servicio.fecha_confirmacion)

    def test_confirmar_sin_stock_no_cambia_el_estado(self):
        self.iniciar_sesion()
        crear_servicio(self.negocio, self.plato, 80, EN_10_DIAS)
        url = reverse("gestion_cotizacion", args=[self.servicio.id])
        self.client.post(url, {"accion": "agregar_articulo", "articulo_id": self.plato.id, "cantidad": 40})
        respuesta = self.client.post(url, {"accion": "confirmar"}, follow=True)
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.estado, Servicio.Estado.COTIZACION)
        self.assertContains(respuesta, "Plato principal: solicitado 40, disponible 20")

    def test_cambiar_a_preparacion_tambien_revisa_stock(self):
        self.iniciar_sesion()
        crear_servicio(self.negocio, self.plato, 80, EN_10_DIAS)
        url = reverse("gestion_cotizacion", args=[self.servicio.id])
        self.client.post(url, {"accion": "agregar_articulo", "articulo_id": self.plato.id, "cantidad": 40})
        self.client.post(url, {"accion": "cambiar_estado", "estado": "Preparacion"})
        self.servicio.refresh_from_db()
        self.assertEqual(self.servicio.estado, Servicio.Estado.COTIZACION)


class DisponibilidadTests(TestCase):
    def setUp(self):
        self.negocio, _, _, self.plato = crear_base()
        self.dia = EN_10_DIAS

    def cotizacion(self, cantidad, inicio=None, fin=None):
        return crear_servicio(
            self.negocio, self.plato, cantidad, inicio or self.dia, fin,
            estado=Servicio.Estado.COTIZACION, rut="222222222",
        )

    def test_1_sin_reservas_superpuestas(self):
        servicio = self.cotizacion(80)
        confirmar_servicio(servicio.id)
        servicio.refresh_from_db()
        self.assertEqual(servicio.estado, Servicio.Estado.CONFIRMADO)

    def test_2_reserva_existente_con_stock_suficiente(self):
        crear_servicio(self.negocio, self.plato, 30, self.dia)
        servicio = self.cotizacion(70)
        confirmar_servicio(servicio.id)
        resultado = calcular_disponibilidad(self.plato, self.dia, self.dia)
        self.assertEqual(resultado["comprometido"], 100)
        self.assertEqual(resultado["disponible"], 0)

    def test_3_la_suma_supera_el_stock(self):
        crear_servicio(self.negocio, self.plato, 70, self.dia)
        servicio = self.cotizacion(40)
        with self.assertRaises(StockInsuficiente) as error:
            confirmar_servicio(servicio.id)
        faltante = error.exception.faltantes[0]
        self.assertEqual((faltante["solicitado"], faltante["disponible"], faltante["faltante"]), (40, 30, 10))
        servicio.refresh_from_db()
        self.assertEqual(servicio.estado, Servicio.Estado.COTIZACION)

    def test_4_reserva_en_otras_fechas_no_afecta(self):
        crear_servicio(self.negocio, self.plato, 90, self.dia + timedelta(days=5), self.dia + timedelta(days=7))
        servicio = self.cotizacion(80, self.dia, self.dia + timedelta(days=2))
        confirmar_servicio(servicio.id)

    def test_rangos_que_se_tocan_si_se_superponen(self):
        crear_servicio(self.negocio, self.plato, 90, self.dia, self.dia + timedelta(days=2))
        servicio = self.cotizacion(80, self.dia + timedelta(days=2), self.dia + timedelta(days=3))
        with self.assertRaises(StockInsuficiente):
            confirmar_servicio(servicio.id)

    def test_5_cotizacion_no_confirmada_no_bloquea(self):
        self.cotizacion(90)
        crear_servicio(self.negocio, self.plato, 90, self.dia, estado=Servicio.Estado.CANCELADO)
        servicio = self.cotizacion(80)
        confirmar_servicio(servicio.id)

    def test_6_el_mismo_servicio_no_se_cuenta_dos_veces(self):
        servicio = crear_servicio(self.negocio, self.plato, 60, self.dia)
        fila = disponibilidad_servicio(servicio)[0]
        self.assertEqual(fila["disponible"], 100)
        self.assertEqual(fila["faltante"], 0)

    def test_7_dos_confirmaciones_seguidas_no_sobrevenden(self):
        primera = self.cotizacion(60)
        segunda = self.cotizacion(60)
        confirmar_servicio(primera.id)
        with self.assertRaises(StockInsuficiente):
            confirmar_servicio(segunda.id)


class InventarioPanelTests(TestCase):
    def setUp(self):
        self.negocio, self.vajilla, _, self.plato = crear_base()
        self.plato.stock_total = self.plato.stock_trabajable = 0
        self.plato.save()
        rol = Rol.objects.create(nombre="Administrador")
        admin = Usuario.objects.create(
            negocio=self.negocio, rol=rol, nombre="Helen", apellido="Cortés", email="a@ejemplo.cl", password="x",
        )
        sesion = self.client.session
        sesion["usuario_id"] = admin.id
        sesion.save()
        self.url = reverse("gestion_producto", args=[self.plato.id])

    def editar(self, **cambios):
        datos = {
            "accion": "editar", "nombre": self.plato.nombre, "categoria_id": self.vajilla.id,
            "precio_arriendo": 500, "stock_total": 40, "stock_trabajable": 40, "activo": "on",
        }
        datos.update(cambios)
        return self.client.post(self.url, datos, follow=True)

    def test_ficha_es_la_del_panel(self):
        respuesta = self.client.get(self.url)
        self.assertTemplateUsed(respuesta, "producto-admin.html")
        self.assertContains(respuesta, "Editar producto y stock")

    def test_editar_stock_vuelve_al_listado_y_registra_movimiento(self):
        respuesta = self.editar(motivo="Compra inicial")
        self.assertRedirects(respuesta, reverse("gestion_inventario"))
        self.assertContains(respuesta, "stock total 40")
        self.plato.refresh_from_db()
        self.assertEqual((self.plato.stock_total, self.plato.stock_trabajable), (40, 40))
        movimiento = self.plato.movimientos.get()
        self.assertEqual((movimiento.tipo, movimiento.cantidad, movimiento.motivo), ("Ajuste", 40, "Compra inicial"))

    def test_stock_invalido_no_se_guarda(self):
        self.editar(stock_total=10, stock_trabajable=20)
        self.editar(stock_total=-5, stock_trabajable=0)
        self.plato.refresh_from_db()
        self.assertEqual(self.plato.stock_total, 0)

    def test_listado_muestra_comprometido_y_disponible(self):
        self.editar()
        servicio = crear_servicio(self.negocio, self.plato, 15, EN_10_DIAS, estado=Servicio.Estado.COTIZACION)
        confirmar_servicio(servicio.id)
        crear_servicio(self.negocio, self.plato, 30, date.today() - timedelta(days=30))  # reserva pasada
        fila = next(a for a in self.client.get(reverse("gestion_inventario")).context["inventario"] if a.id == self.plato.id)
        self.assertEqual((fila.stock_comprometido, fila.stock_disponible), (15, 25))

    def test_crear_producto_con_codigo_automatico(self):
        respuesta = self.client.post(reverse("gestion_inventario"), {
            "accion": "crear", "nombre": "Copa flauta", "categoria_id": self.vajilla.id,
            "precio_arriendo": 300, "stock_total": 50, "stock_trabajable": 50, "activo": "on",
        }, follow=True)
        articulo = Articulo.objects.get(nombre="Copa flauta")
        self.assertTrue(articulo.codigo)
        self.assertContains(respuesta, "Producto Copa flauta creado")
        self.assertEqual(articulo.movimientos.get().cantidad, 50)

    def test_resumen_y_reportes_cuentan_solo_reservas(self):
        self.editar()
        cotizacion = crear_servicio(self.negocio, self.plato, 10, EN_10_DIAS, estado=Servicio.Estado.COTIZACION)
        respuesta = self.client.get(reverse("inicio"))
        self.assertEqual(respuesta.context["productos_mas_arrendados"], [])
        confirmar_servicio(cotizacion.id)
        respuesta = self.client.get(reverse("inicio"))
        self.assertEqual(respuesta.context["productos_mas_arrendados"][0]["cantidad"], 10)
        self.assertEqual(len(respuesta.context["proximos_eventos"]), 1)
        self.assertNotContains(respuesta, "Datos de ejemplo")
        self.assertEqual(respuesta.context["articulos_disponibles"], 30)
        reportes = self.client.get(reverse("gestion_reportes"))
        self.assertEqual(reportes.context["metricas"]["reservas_total"], 1)


@unittest.skipUnless(connection.vendor == "postgresql", "Requiere PostgreSQL para probar bloqueos reales.")
class ConcurrenciaTests(TransactionTestCase):
    """Caso 7 con dos hilos reales: solo una de las dos confirmaciones puede pasar."""

    def test_confirmaciones_simultaneas(self):
        negocio, _, _, plato = crear_base()
        servicios = [
            crear_servicio(negocio, plato, 60, EN_10_DIAS, estado=Servicio.Estado.COTIZACION, rut=f"{i}11111111")
            for i in (1, 2)
        ]
        resultados = []
        barrera = threading.Barrier(2)

        def confirmar(servicio_id):
            barrera.wait()
            try:
                confirmar_servicio(servicio_id)
                resultados.append("ok")
            except StockInsuficiente:
                resultados.append("sin stock")
            finally:
                connection.close()

        hilos = [threading.Thread(target=confirmar, args=(s.id,)) for s in servicios]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
        self.assertEqual(sorted(resultados), ["ok", "sin stock"])
