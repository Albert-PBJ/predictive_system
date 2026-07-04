"""Registro de operaciones importables/exportables (continuidad operativa).

Cada ``Handler`` describe una operación (venta, movimiento de inventario, cliente,
presupuesto): sus columnas de Excel, cómo agrupar las filas en registros, cómo
detectar duplicados y — sobre todo — cómo **crearla reutilizando el servicio de
negocio existente** (`create_sale`, `apply_movement`, `create_quote`, …), de modo que
una operación importada valida stock, aplica el costo promedio, descuenta inventario y
respeta exactamente las mismas reglas que si se registrara desde la interfaz.

Idempotencia: cada registro lleva una **referencia offline** (columna «Referencia», o
el RIF para clientes). Antes de crear, el servicio descarta las referencias ya
presentes en la BD (`existing_refs`) — así reimportar el mismo archivo no duplica nada.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.permissions import IsOperational, IsSeller, IsWarehouse
from apps.audit.models import CategoryChoices
from apps.core.models import Customer, Product, Seller
from apps.inventory.models import InventoryMovement
from apps.inventory.services import apply_movement
from apps.sales.models import Quote, Sale
from apps.sales.services import create_quote, create_sale

from .schema import Column, _as_text, coerce

# Prefijo reservado para las referencias que **genera el sistema** en el export de
# registros que no vienen de una importación (p. ej. SYS-VENTA-42). Reimportar un export
# reintroduciría esos registros como nuevos, así que la importación **rechaza** cualquier
# «Referencia» que empiece con este prefijo. Un usuario no debe usarlo en su archivo offline.
RESERVED_REF_PREFIX = "SYS-"


class ImportRowError(Exception):
    """Error de negocio en una fila/registro concreto (se muestra al usuario)."""


@dataclass
class Record:
    """Un registro a importar: una operación completa (agrupa varias filas si aplica)."""

    ref: str
    row_label: str
    header: dict
    lines: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class Handler:
    entity: str
    label: str
    grouped: bool = False
    columns: list[Column] = []
    reference_sheets: list[str] = []
    dedup_key: str = "referencia"
    audit_category = CategoryChoices.CONFIG
    import_permission = IsSeller
    export_permission = IsOperational
    instructions: list[str] = []

    # --- columnas ---------------------------------------------------------- #
    def header_columns(self) -> list[Column]:
        return [c for c in self.columns if not c.line]

    def line_columns(self) -> list[Column]:
        return [c for c in self.columns if c.line]

    def column_by_key(self, key) -> Column | None:
        return next((c for c in self.columns if c.key == key), None)

    def _dedup_header(self) -> str:
        col = self.column_by_key(self.dedup_key)
        return col.header if col else self.dedup_key

    # --- parseo/coerción --------------------------------------------------- #
    def _coerce(self, raw: dict, cols: list[Column]) -> tuple[dict, list[str]]:
        out, errors = {}, []
        for c in cols:
            val, err = coerce(c, raw.get(c.key))
            out[c.key] = val
            if err:
                errors.append(err)
        return out, errors

    def build_records(self, rows: list[dict]) -> list[Record]:
        """Convierte las filas crudas (por clave de columna) en registros coercidos."""
        if not self.grouped:
            records = []
            for raw in rows:
                fields, errs = self._coerce(raw, self.columns)
                rec = Record(ref=_as_text(raw.get(self.dedup_key)), row_label=str(raw["_row"]), header=fields)
                rec.errors.extend(errs)
                records.append(rec)
            return records

        # Agrupadas (ventas/presupuestos): varias filas con la misma referencia = 1 operación.
        groups: "OrderedDict[str, list[dict]]" = OrderedDict()
        orphans = []
        for raw in rows:
            ref = _as_text(raw.get(self.dedup_key))
            if not ref:
                orphans.append(raw)
            else:
                groups.setdefault(ref, []).append(raw)

        records = []
        for raw in orphans:
            header, herr = self._coerce(raw, self.header_columns())
            rec = Record(ref="", row_label=str(raw["_row"]), header=header)
            rec.errors.append(f"Falta «{self._dedup_header()}» (obligatorio para agrupar la operación).")
            rec.errors.extend(herr)
            records.append(rec)

        for ref, grp in groups.items():
            header, herr = self._coerce(grp[0], self.header_columns())
            lines, lerr = [], []
            for raw in grp:
                line, e = self._coerce(raw, self.line_columns())
                lines.append(line)
                lerr.extend(e)
            label = str(grp[0]["_row"]) if len(grp) == 1 else f"{grp[0]['_row']}–{grp[-1]['_row']}"
            rec = Record(ref=ref, row_label=label, header=header, lines=lines)
            rec.errors.extend(herr)
            rec.errors.extend(lerr)
            records.append(rec)
        return records

    # --- contexto/dedup ---------------------------------------------------- #
    def build_context(self, records: list[Record]) -> dict:
        return {}

    def existing_refs(self, refs: set[str]) -> set[str]:
        return set()

    def reserved_refs(self, refs: set[str]) -> dict[str, str]:
        """Referencias que la importación debe rechazar por ser **del sistema**.

        Cubre dos casos: (1) el prefijo reservado ``SYS-`` que emite el export para
        registros no importados, y (2) coincidencias con un identificador natural ya
        generado por el sistema (nº de factura, nº de presupuesto) — ver
        ``_natural_collisions`` por operación. Devuelve ``{ref: motivo}``.
        """
        out = {}
        for ref in refs:
            if ref and ref.upper().startswith(RESERVED_REF_PREFIX):
                out[ref] = (
                    f"«{ref}» es una referencia reservada del sistema (la genera el export). "
                    "No se puede importar: usa una referencia propia para tu operación offline."
                )
        for ref, reason in self._natural_collisions(refs).items():
            out.setdefault(ref, reason)
        return out

    def _natural_collisions(self, refs: set[str]) -> dict[str, str]:
        return {}

    # --- creación (a implementar) ----------------------------------------- #
    def create(self, record: Record, ctx: dict, user):
        raise NotImplementedError

    # --- exportación ------------------------------------------------------- #
    def export_rows(self, params, user) -> list[dict]:
        return []

    # --- helpers compartidos ---------------------------------------------- #
    @staticmethod
    def _products_by_sku(skus):
        skus = {s for s in skus if s}
        return {p.sku: p for p in Product.objects.filter(sku__in=skus)}

    @staticmethod
    def _customers_by_rif(rifs):
        rifs = {r for r in rifs if r}
        return {c.rif: c for c in Customer.objects.filter(rif__in=rifs)}

    @staticmethod
    def _sellers_by_username(usernames):
        usernames = {u for u in usernames if u}
        out = {}
        for u in User.objects.filter(username__in=usernames).select_related("seller_profile"):
            out[u.username] = getattr(u, "seller_profile", None)
        return out

    def _resolve_seller(self, username, ctx, user, allow_none=False):
        if username:
            seller = ctx.get("sellers", {}).get(username)
            if seller is None:
                raise ImportRowError(f"El usuario vendedor «{username}» no existe o no tiene perfil de vendedor.")
            return seller
        seller = getattr(user, "seller_profile", None)
        if seller is None and not allow_none:
            raise ImportRowError(
                "No se pudo determinar el vendedor: tu usuario no tiene perfil de vendedor. "
                "Indica la columna «Usuario vendedor» en el archivo."
            )
        return seller


def _date_filter(qs, params, field):
    d_from = (params.get("from") or "").strip()
    d_to = (params.get("to") or "").strip()
    if d_from:
        qs = qs.filter(**{f"{field}__gte": d_from})
    if d_to:
        qs = qs.filter(**{f"{field}__lte": d_to})
    return qs


# --------------------------------------------------------------------------- #
# Ventas
# --------------------------------------------------------------------------- #
_SALE_TYPE_CHOICES = {
    "detal": Sale.TypeChoices.RETAIL, "det": Sale.TypeChoices.RETAIL,
    "ret": Sale.TypeChoices.RETAIL, "retail": Sale.TypeChoices.RETAIL,
    "institucional": Sale.TypeChoices.INSTITUTIONAL, "inst": Sale.TypeChoices.INSTITUTIONAL,
    "proyecto": Sale.TypeChoices.INSTITUTIONAL, "proyecto institucional": Sale.TypeChoices.INSTITUTIONAL,
}


class SalesHandler(Handler):
    entity = "sales"
    label = "Ventas"
    grouped = True
    dedup_key = "referencia"
    audit_category = CategoryChoices.VENTAS
    import_permission = IsSeller
    reference_sheets = ["productos", "clientes", "vendedores"]
    instructions = [
        "Cada VENTA se identifica con una «Referencia» única (p. ej. V-001). Para una venta de "
        "varios productos, repite la misma Referencia en varias filas: una fila por producto.",
        "Las columnas de cabecera (Fecha, RIF Cliente, Tipo, Usuario vendedor, Notas) se toman de la "
        "primera fila de cada Referencia; las demás filas del grupo solo necesitan Producto y Cantidad.",
        "El Descuento % se aplica sobre el precio de lista del producto. Si prefieres, indica el "
        "«Precio unitario USD» y el sistema calculará el descuento equivalente.",
        "El cliente (por RIF) y el producto (por SKU) deben existir. Consulta las hojas «Clientes» y "
        "«Productos». Si el cliente es nuevo, impórtalo primero desde el módulo Clientes.",
        "La venta descuenta inventario y aplica el costo promedio, igual que una venta normal.",
    ]
    columns = [
        Column("referencia", "Referencia", required=True, help="Código único de la venta (agrupa sus líneas)."),
        Column("fecha", "Fecha", kind="date", required=True, help="Fecha de la venta (AAAA-MM-DD)."),
        Column("cliente_rif", "RIF Cliente", required=True, help="RIF de un cliente existente."),
        Column("producto_sku", "SKU Producto", required=True, line=True, help="SKU de un producto existente."),
        Column("cantidad", "Cantidad", kind="int", required=True, line=True),
        Column("descuento_pct", "Descuento %", kind="decimal", line=True, help="Opcional. 0 si no hay descuento."),
        Column("precio_unitario_usd", "Precio unitario USD", kind="decimal", line=True,
               help="Opcional. Si se indica, tiene prioridad sobre el descuento."),
        Column("tipo_venta", "Tipo", kind="choice", choices=_SALE_TYPE_CHOICES,
               dropdown=["Detal", "Institucional"], help="Detal o Institucional (por defecto Detal)."),
        Column("vendedor_usuario", "Usuario vendedor",
               help="Opcional. Usuario del vendedor; por defecto, quien importa."),
        Column("notas", "Notas"),
    ]

    def build_context(self, records):
        skus, rifs, users = set(), set(), set()
        for r in records:
            rifs.add(r.header.get("cliente_rif"))
            users.add(r.header.get("vendedor_usuario"))
            for ln in r.lines:
                skus.add(ln.get("producto_sku"))
        return {
            "products": self._products_by_sku(skus),
            "customers": self._customers_by_rif(rifs),
            "sellers": self._sellers_by_username(users),
        }

    def existing_refs(self, refs):
        return set(
            Sale.objects.filter(import_ref__in=refs).values_list("import_ref", flat=True)
        )

    def _natural_collisions(self, refs):
        hits = (
            Sale.objects.filter(invoice_number__in=refs)
            .exclude(invoice_number__isnull=True)
            .values_list("invoice_number", flat=True)
        )
        return {
            v: f"«{v}» coincide con un número de factura ya registrado en el sistema. Usa una referencia propia."
            for v in hits
        }

    def create(self, record, ctx, user):
        h = record.header
        customer = ctx["customers"].get(h["cliente_rif"])
        if customer is None:
            raise ImportRowError(
                f"El cliente con RIF «{h['cliente_rif']}» no existe. Impórtalo primero desde Clientes."
            )
        seller = self._resolve_seller(h.get("vendedor_usuario"), ctx, user)

        if not record.lines:
            raise ImportRowError("La venta no tiene líneas de producto.")
        items = []
        for ln in record.lines:
            product = ctx["products"].get(ln.get("producto_sku"))
            if product is None:
                raise ImportRowError(f"El producto con SKU «{ln.get('producto_sku')}» no existe.")
            item = {"product": product.id, "quantity": ln["cantidad"]}
            if ln.get("precio_unitario_usd") is not None:
                item["unit_sale_price_usd"] = ln["precio_unitario_usd"]
            elif ln.get("descuento_pct") is not None:
                item["discount_pct"] = ln["descuento_pct"]
            items.append(item)

        sale = create_sale(
            seller=seller,
            customer=customer,
            items=items,
            user=user,
            sale_date=h["fecha"],
            sale_type=h.get("tipo_venta") or Sale.TypeChoices.RETAIL,
            status=Sale.StatusChoices.COMPLETED,
            notes=h.get("notas") or "",
        )
        sale.import_ref = record.ref
        sale.save(update_fields=["import_ref"])
        return sale, f"{len(items)} línea(s) · {customer.company_name} · ${sale.total_sale_usd}"

    def export_rows(self, params, user):
        qs = (
            Sale.objects.select_related("customer", "seller__user")
            .prefetch_related("items__product")
            .order_by("-sale_date", "-id")
        )
        status = (params.get("status") or "").strip()
        if status:
            qs = qs.filter(status=status)
        qs = _date_filter(qs, params, "sale_date")
        rows = []
        for s in qs[:5000]:
            # Referencia = la de importación si existe; si no, una reservada del sistema
            # (no reimportable). El nº de factura no se emite aquí para no re-crear la venta.
            ref = s.import_ref or f"{RESERVED_REF_PREFIX}VENTA-{s.id}"
            username = s.seller.user.username if s.seller and s.seller.user else ""
            for it in s.items.all():
                rows.append({
                    "referencia": ref,
                    "fecha": s.sale_date.isoformat(),
                    "cliente_rif": s.customer.rif,
                    "producto_sku": it.product.sku or "",
                    "cantidad": it.quantity,
                    "descuento_pct": it.discount_pct,
                    "precio_unitario_usd": it.unit_sale_price_usd,
                    "tipo_venta": s.get_sale_type_display(),
                    "vendedor_usuario": username,
                    "notas": s.notes or "",
                })
        return rows


# --------------------------------------------------------------------------- #
# Movimientos de inventario
# --------------------------------------------------------------------------- #
_MOV_TYPE_CHOICES = {
    "ent": InventoryMovement.MovementTypeChoices.ENTRY, "entrada": InventoryMovement.MovementTypeChoices.ENTRY,
    "compra": InventoryMovement.MovementTypeChoices.ENTRY, "reposicion": InventoryMovement.MovementTypeChoices.ENTRY,
    "aju": InventoryMovement.MovementTypeChoices.ADJUSTMENT, "ajuste": InventoryMovement.MovementTypeChoices.ADJUSTMENT,
    "dev": InventoryMovement.MovementTypeChoices.RETURN, "devolucion": InventoryMovement.MovementTypeChoices.RETURN,
}


class InventoryHandler(Handler):
    entity = "inventory"
    label = "Movimientos"
    grouped = False
    dedup_key = "referencia"
    audit_category = CategoryChoices.INVENTARIO
    import_permission = IsWarehouse
    reference_sheets = ["productos"]
    instructions = [
        "Una fila por movimiento. La «Referencia» es un código único del movimiento (p. ej. E-001); "
        "evita que se duplique si reimportas el archivo.",
        "Tipo: ENT (entrada/compra), AJU (ajuste) o DEV (devolución). Para ENT y DEV la cantidad es "
        "positiva; para AJU usa cantidad negativa para disminuir (merma) o positiva para aumentar.",
        "El «Costo unitario USD» solo aplica a las entradas (ENT): recalcula el costo promedio "
        "ponderado del producto. Déjalo vacío para conservar el promedio actual.",
        "Las salidas por venta (SAL) NO se cargan aquí: las genera el módulo de ventas.",
    ]
    columns = [
        Column("referencia", "Referencia", required=True, help="Código único del movimiento."),
        Column("fecha", "Fecha", kind="date", required=True),
        Column("producto_sku", "SKU Producto", required=True),
        Column("tipo", "Tipo", kind="choice", required=True, choices=_MOV_TYPE_CHOICES,
               dropdown=["ENT", "AJU", "DEV"], help="ENT, AJU o DEV."),
        Column("cantidad", "Cantidad", kind="int", required=True,
               help="Positiva para ENT/DEV; negativa permitida solo en AJU."),
        Column("costo_unitario_usd", "Costo unitario USD", kind="decimal",
               help="Solo ENT: recalcula el costo promedio ponderado."),
        Column("notas", "Notas"),
    ]

    def build_context(self, records):
        skus = {r.header.get("producto_sku") for r in records}
        return {"products": self._products_by_sku(skus)}

    def existing_refs(self, refs):
        return set(
            InventoryMovement.objects.filter(import_ref__in=refs).values_list("import_ref", flat=True)
        )

    def create(self, record, ctx, user):
        h = record.header
        product = ctx["products"].get(h["producto_sku"])
        if product is None:
            raise ImportRowError(f"El producto con SKU «{h['producto_sku']}» no existe.")
        if product.is_service:
            raise ImportRowError(f"«{product.name}» es un servicio: no lleva inventario.")
        mtype = h["tipo"]
        qty = h["cantidad"]
        if mtype in (InventoryMovement.MovementTypeChoices.ENTRY, InventoryMovement.MovementTypeChoices.RETURN) and qty <= 0:
            raise ImportRowError("Para entradas (ENT) y devoluciones (DEV) la cantidad debe ser positiva.")
        if mtype == InventoryMovement.MovementTypeChoices.ADJUSTMENT and qty == 0:
            raise ImportRowError("El ajuste (AJU) no puede tener cantidad cero.")

        movement = apply_movement(
            product=product,
            movement_type=mtype,
            quantity=qty,
            responsible=user,
            reference=record.ref,
            notes=h.get("notas") or "",
            movement_date=h["fecha"],
            unit_cost=h.get("costo_unitario_usd"),
        )
        movement.import_ref = record.ref
        movement.save(update_fields=["import_ref"])
        return movement, f"{product.name} · {movement.get_movement_type_display()} {qty:+d}"

    def export_rows(self, params, user):
        qs = InventoryMovement.objects.select_related("product").order_by("-movement_date", "-id")
        mtype = (params.get("movement_type") or "").strip()
        if mtype:
            qs = qs.filter(movement_type=mtype)
        qs = _date_filter(qs, params, "movement_date")
        rows = []
        for m in qs[:5000]:
            rows.append({
                "referencia": m.import_ref or f"{RESERVED_REF_PREFIX}MOV-{m.id}",
                "fecha": m.movement_date.isoformat(),
                "producto_sku": m.product.sku or "",
                "tipo": m.get_movement_type_display(),
                "cantidad": m.quantity,
                "costo_unitario_usd": m.unit_cost_usd if m.unit_cost_usd is not None else "",
                "notas": m.notes or "",
            })
        return rows


# --------------------------------------------------------------------------- #
# Clientes
# --------------------------------------------------------------------------- #
_CUST_TYPE_CHOICES = {
    "inst": Customer.TypeChoices.INSTITUTIONAL, "institucional": Customer.TypeChoices.INSTITUTIONAL,
    "corp": Customer.TypeChoices.CORPORATE, "empresarial": Customer.TypeChoices.CORPORATE,
    "ind": Customer.TypeChoices.INDIVIDUAL, "particular": Customer.TypeChoices.INDIVIDUAL,
}


class CustomersHandler(Handler):
    entity = "customers"
    label = "Clientes"
    grouped = False
    dedup_key = "rif"
    audit_category = CategoryChoices.CLIENTES
    import_permission = IsSeller
    reference_sheets = []
    instructions = [
        "Una fila por cliente. El RIF es la clave: si ya existe un cliente con ese RIF, la fila se "
        "omite (no se duplica ni se sobrescribe).",
        "Tipo: Institucional, Empresarial o Particular (por defecto Empresarial).",
        "«Activo» = SI para un cliente ya activo; NO (o vacío) para un prospecto.",
    ]
    columns = [
        Column("rif", "RIF", required=True, help="RIF/cédula fiscal (clave única)."),
        Column("company_name", "Razón social", required=True),
        Column("customer_type", "Tipo", kind="choice", choices=_CUST_TYPE_CHOICES,
               dropdown=["Institucional", "Empresarial", "Particular"]),
        Column("sector", "Sector"),
        Column("contact_first_name", "Nombre contacto"),
        Column("contact_last_name", "Apellido contacto"),
        Column("contact_ci", "Cédula contacto"),
        Column("phone", "Teléfono"),
        Column("mobile", "Móvil"),
        Column("email", "Email"),
        Column("state", "Estado"),
        Column("municipality", "Municipio"),
        Column("fiscal_address", "Dirección fiscal"),
        Column("is_active_customer", "Activo", kind="bool", dropdown=["SI", "NO"]),
    ]

    def existing_refs(self, refs):
        return set(Customer.objects.filter(rif__in=refs).values_list("rif", flat=True))

    def create(self, record, ctx, user):
        h = record.header
        customer = Customer.objects.create(
            rif=h["rif"],
            company_name=h["company_name"],
            customer_type=h.get("customer_type") or Customer.TypeChoices.CORPORATE,
            sector=h.get("sector") or "",
            contact_first_name=h.get("contact_first_name") or "",
            contact_last_name=h.get("contact_last_name") or "",
            contact_ci=h.get("contact_ci") or "",
            phone=h.get("phone") or "",
            mobile=h.get("mobile") or "",
            email=h.get("email") or "",
            state=h.get("state") or "",
            municipality=h.get("municipality") or "",
            fiscal_address=h.get("fiscal_address") or "",
            is_active_customer=bool(h.get("is_active_customer")),
        )
        return customer, f"{customer.company_name} (RIF {customer.rif})"

    def export_rows(self, params, user):
        qs = Customer.objects.all().order_by("company_name")
        ctype = (params.get("customer_type") or "").strip()
        if ctype:
            qs = qs.filter(customer_type=ctype)
        rows = []
        for c in qs[:10000]:
            rows.append({
                "rif": c.rif,
                "company_name": c.company_name,
                "customer_type": c.get_customer_type_display(),
                "sector": c.sector or "",
                "contact_first_name": c.contact_first_name or "",
                "contact_last_name": c.contact_last_name or "",
                "contact_ci": c.contact_ci or "",
                "phone": c.phone or "",
                "mobile": c.mobile or "",
                "email": c.email or "",
                "state": c.state or "",
                "municipality": c.municipality or "",
                "fiscal_address": c.fiscal_address or "",
                "is_active_customer": "SI" if c.is_active_customer else "NO",
            })
        return rows


# --------------------------------------------------------------------------- #
# Presupuestos
# --------------------------------------------------------------------------- #
_QUOTE_STATUS_CHOICES = {
    "borrador": Quote.StatusChoices.DRAFT, "dra": Quote.StatusChoices.DRAFT,
    "enviado": Quote.StatusChoices.SENT, "sen": Quote.StatusChoices.SENT,
    "aprobado": Quote.StatusChoices.APPROVED, "apr": Quote.StatusChoices.APPROVED,
    "rechazado": Quote.StatusChoices.REJECTED, "rej": Quote.StatusChoices.REJECTED,
}


class QuotesHandler(Handler):
    entity = "quotes"
    label = "Presupuestos"
    grouped = True
    dedup_key = "referencia"
    audit_category = CategoryChoices.VENTAS
    import_permission = IsSeller
    reference_sheets = ["productos", "clientes", "vendedores"]
    instructions = [
        "Cada PRESUPUESTO se identifica con una «Referencia» única. Repite la misma Referencia en "
        "varias filas para un presupuesto de varios productos (una fila por producto).",
        "La cabecera (Fecha emisión, RIF Cliente, IVA %, Vencimiento, Usuario vendedor, Estado) se "
        "toma de la primera fila del grupo.",
        "El «Precio unitario USD» es opcional: si se omite, se usa el precio de venta actual del "
        "producto. Un presupuesto NO toca inventario.",
        "El cliente (por RIF) y el producto (por SKU) deben existir (ver hojas de referencia).",
    ]
    columns = [
        Column("referencia", "Referencia", required=True, help="Código único del presupuesto."),
        Column("fecha_emision", "Fecha emisión", kind="date", required=True),
        Column("cliente_rif", "RIF Cliente", required=True),
        Column("producto_sku", "SKU Producto", required=True, line=True),
        Column("cantidad", "Cantidad", kind="int", required=True, line=True),
        Column("precio_unitario_usd", "Precio unitario USD", kind="decimal", line=True,
               help="Opcional. Por defecto, el precio de venta del producto."),
        Column("iva_pct", "IVA %", kind="decimal", help="Opcional (por defecto 16%)."),
        Column("vencimiento", "Vencimiento", kind="date", help="Opcional."),
        Column("vendedor_usuario", "Usuario vendedor", help="Opcional."),
        Column("estado", "Estado", kind="choice", choices=_QUOTE_STATUS_CHOICES,
               dropdown=["Borrador", "Enviado", "Aprobado", "Rechazado"],
               help="Por defecto Enviado."),
    ]

    def build_context(self, records):
        skus, rifs, users = set(), set(), set()
        for r in records:
            rifs.add(r.header.get("cliente_rif"))
            users.add(r.header.get("vendedor_usuario"))
            for ln in r.lines:
                skus.add(ln.get("producto_sku"))
        return {
            "products": self._products_by_sku(skus),
            "customers": self._customers_by_rif(rifs),
            "sellers": self._sellers_by_username(users),
        }

    def existing_refs(self, refs):
        return set(Quote.objects.filter(import_ref__in=refs).values_list("import_ref", flat=True))

    def _natural_collisions(self, refs):
        hits = Quote.objects.filter(quote_number__in=refs).values_list("quote_number", flat=True)
        return {
            v: f"«{v}» coincide con un número de presupuesto ya registrado en el sistema. Usa una referencia propia."
            for v in hits
        }

    def create(self, record, ctx, user):
        h = record.header
        customer = ctx["customers"].get(h["cliente_rif"])
        if customer is None:
            raise ImportRowError(
                f"El cliente con RIF «{h['cliente_rif']}» no existe. Impórtalo primero desde Clientes."
            )
        seller = self._resolve_seller(h.get("vendedor_usuario"), ctx, user, allow_none=True)

        if not record.lines:
            raise ImportRowError("El presupuesto no tiene líneas de producto.")
        items = []
        for ln in record.lines:
            product = ctx["products"].get(ln.get("producto_sku"))
            if product is None:
                raise ImportRowError(f"El producto con SKU «{ln.get('producto_sku')}» no existe.")
            items.append({
                "product": product.id,
                "quantity": ln["cantidad"],
                "unit_price_usd": ln.get("precio_unitario_usd"),
            })

        quote = create_quote(
            seller=seller,
            customer=customer,
            items=items,
            issued_date=h["fecha_emision"],
            expiry_date=h.get("vencimiento"),
            iva_rate=h.get("iva_pct"),
            status=h.get("estado") or Quote.StatusChoices.SENT,
        )
        quote.import_ref = record.ref
        quote.save(update_fields=["import_ref"])
        return quote, f"{len(items)} línea(s) · {customer.company_name} · ${quote.total_usd}"

    def export_rows(self, params, user):
        qs = (
            Quote.objects.select_related("customer", "seller__user")
            .prefetch_related("items__product")
            .order_by("-issued_date", "-id")
        )
        status = (params.get("status") or "").strip()
        if status:
            qs = qs.filter(status=status)
        qs = _date_filter(qs, params, "issued_date")
        rows = []
        for q in qs[:5000]:
            ref = q.import_ref or f"{RESERVED_REF_PREFIX}PRES-{q.id}"
            username = q.seller.user.username if q.seller and q.seller.user else ""
            for it in q.items.all():
                rows.append({
                    "referencia": ref,
                    "fecha_emision": q.issued_date.isoformat(),
                    "cliente_rif": q.customer.rif,
                    "producto_sku": it.product.sku or "",
                    "cantidad": it.quantity,
                    "precio_unitario_usd": it.unit_price_usd,
                    "iva_pct": q.iva_rate,
                    "vencimiento": q.expiry_date.isoformat() if q.expiry_date else "",
                    "vendedor_usuario": username,
                    "estado": q.get_status_display(),
                })
        return rows


HANDLERS = {h.entity: h() for h in (SalesHandler, InventoryHandler, CustomersHandler, QuotesHandler)}


def get_handler(entity) -> Handler | None:
    return HANDLERS.get(entity)


def reference_sheet_rows(kind):
    """Filas para las hojas de referencia de la plantilla (catálogos de apoyo)."""
    if kind == "productos":
        rows = [["SKU", "Nombre", "Categoría", "Precio venta USD", "Stock"]]
        qs = Product.objects.filter(is_active=True).select_related("category").order_by("category__name", "name")
        for p in qs[:5000]:
            rows.append([p.sku or "", p.name, p.category.name if p.category else "—",
                         float(p.sale_price_usd or 0), p.stock])
        return rows
    if kind == "clientes":
        rows = [["RIF", "Razón social", "Tipo"]]
        for c in Customer.objects.all().order_by("company_name")[:10000]:
            rows.append([c.rif, c.company_name, c.get_customer_type_display()])
        return rows
    if kind == "vendedores":
        rows = [["Usuario", "Nombre"]]
        for s in Seller.objects.filter(is_active=True).select_related("user"):
            if s.user:
                rows.append([s.user.username, f"{s.first_name} {s.last_name}".strip()])
        return rows
    return []
