from django.db import models
from django.utils.translation import gettext_lazy as _


class ActionChoices(models.TextChoices):
    """Acciones auditables del sistema.

    Cada acción describe un hecho de negocio relevante (quién hizo qué). El valor es
    un código estable en inglés; la etiqueta es el texto que ve el administrador.
    """

    SALE_CREATE = "SALE_CREATE", _("Venta registrada")
    SALE_VOID = "SALE_VOID", _("Venta anulada")
    QUOTE_CREATE = "QUOTE_CREATE", _("Presupuesto creado")
    INVENTORY_MOVEMENT = "INVENTORY_MOVEMENT", _("Movimiento de inventario")
    SCRAPE_START = "SCRAPE_START", _("Scraping iniciado")
    REPORT_GENERATE = "REPORT_GENERATE", _("Reporte ejecutivo generado")
    USER_CREATE = "USER_CREATE", _("Usuario creado")
    SETTINGS_UPDATE = "SETTINGS_UPDATE", _("Configuración actualizada")
    RATE_UPDATE = "RATE_UPDATE", _("Tasa de cambio cargada")
    PRODUCT_CREATE = "PRODUCT_CREATE", _("Producto creado")
    PRODUCT_UPDATE = "PRODUCT_UPDATE", _("Producto actualizado")
    CUSTOMER_CREATE = "CUSTOMER_CREATE", _("Cliente creado")
    CUSTOMER_UPDATE = "CUSTOMER_UPDATE", _("Cliente actualizado")
    LOGIN = "LOGIN", _("Inicio de sesión")
    LOGOUT = "LOGOUT", _("Cierre de sesión")
    LOG_PURGE = "LOG_PURGE", _("Registros de auditoría purgados")


class CategoryChoices(models.TextChoices):
    """Agrupación temática de las acciones, para filtrar el registro por área."""

    VENTAS = "VENTAS", _("Ventas")
    INVENTARIO = "INVENTARIO", _("Inventario")
    SCRAPERS = "SCRAPERS", _("Datos externos")
    REPORTES = "REPORTES", _("Reportes")
    USUARIOS = "USUARIOS", _("Usuarios")
    CATALOGO = "CATALOGO", _("Catálogo")
    CLIENTES = "CLIENTES", _("Clientes")
    CONFIG = "CONFIG", _("Configuración")
    AUTH = "AUTH", _("Autenticación")


# Categoría por defecto de cada acción (el servicio la usa si no se indica una).
ACTION_CATEGORY = {
    ActionChoices.SALE_CREATE: CategoryChoices.VENTAS,
    ActionChoices.SALE_VOID: CategoryChoices.VENTAS,
    ActionChoices.QUOTE_CREATE: CategoryChoices.VENTAS,
    ActionChoices.INVENTORY_MOVEMENT: CategoryChoices.INVENTARIO,
    ActionChoices.SCRAPE_START: CategoryChoices.SCRAPERS,
    ActionChoices.REPORT_GENERATE: CategoryChoices.REPORTES,
    ActionChoices.USER_CREATE: CategoryChoices.USUARIOS,
    ActionChoices.SETTINGS_UPDATE: CategoryChoices.CONFIG,
    ActionChoices.RATE_UPDATE: CategoryChoices.CONFIG,
    ActionChoices.PRODUCT_CREATE: CategoryChoices.CATALOGO,
    ActionChoices.PRODUCT_UPDATE: CategoryChoices.CATALOGO,
    ActionChoices.CUSTOMER_CREATE: CategoryChoices.CLIENTES,
    ActionChoices.CUSTOMER_UPDATE: CategoryChoices.CLIENTES,
    ActionChoices.LOGIN: CategoryChoices.AUTH,
    ActionChoices.LOGOUT: CategoryChoices.AUTH,
    ActionChoices.LOG_PURGE: CategoryChoices.CONFIG,
}


class AuditLog(models.Model):
    """Bitácora de auditoría: un registro inmutable por acción relevante del sistema.

    Responde "qué pasó, quién lo hizo y cuándo". Es **append-only**: las filas no se
    editan (solo se consultan y, eventualmente, se purgan por antigüedad). Para que el
    rastro sobreviva al borrado o cambio de rol de un usuario, se guarda una *foto* del
    nombre de usuario y el rol al momento del hecho, además del FK al usuario.
    """

    actor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text=_("Usuario que ejecutó la acción (nulo si la realizó el sistema)"),
    )
    actor_username = models.CharField(
        max_length=150, blank=True, help_text=_("Nombre de usuario al momento del hecho (foto)")
    )
    actor_role = models.CharField(
        max_length=10, blank=True, help_text=_("Rol del usuario al momento del hecho (foto)")
    )

    action = models.CharField(
        max_length=32, choices=ActionChoices.choices, help_text=_("Acción auditada")
    )
    category = models.CharField(
        max_length=12, choices=CategoryChoices.choices, help_text=_("Área a la que pertenece la acción")
    )
    description = models.CharField(
        max_length=500, help_text=_("Resumen legible de lo ocurrido, en español")
    )

    # Objeto afectado (genérico, sin FK estricto para no atar el log a un modelo).
    target_model = models.CharField(max_length=50, blank=True, help_text=_("Modelo del objeto afectado"))
    target_id = models.CharField(max_length=50, blank=True, help_text=_("Identificador del objeto afectado"))

    metadata = models.JSONField(
        default=dict, blank=True, help_text=_("Detalles estructurados adicionales (montos, conteos, etc.)")
    )
    ip_address = models.GenericIPAddressField(
        null=True, blank=True, help_text=_("Dirección IP desde la que se ejecutó la acción")
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "audit_logs"
        verbose_name = "Registro de Auditoría"
        verbose_name_plural = "Registros de Auditoría"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["category", "created_at"], name="audit_cat_date_idx"),
            models.Index(fields=["action"], name="audit_action_idx"),
            models.Index(fields=["actor"], name="audit_actor_idx"),
        ]

    def __str__(self):
        who = self.actor_username or "sistema"
        return f"{self.get_action_display()} — {who} ({self.created_at:%Y-%m-%d %H:%M})"
