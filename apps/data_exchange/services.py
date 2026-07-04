"""Orquestación de import/export (continuidad operativa).

`run_import` recorre los registros del archivo y, por cada uno, intenta crearlo con el
servicio de negocio real dentro de una transacción propia — así una fila con error no
tumba a las demás. En modo **preview** (``commit=False``) se ejecuta exactamente la
misma creación pero se **revierte** (rollback) para validar de verdad (stock, cliente,
producto…) sin persistir nada. Los duplicados (referencia ya en la BD, o repetida en el
archivo) se omiten para que reimportar sea idempotente.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.inventory.services import InsufficientStockError
from apps.sales.services import QuoteValidationError, SaleValidationError

from .excel_io import build_export_workbook, build_template_workbook, read_entity_sheet
from .handlers import ImportRowError, get_handler

# Errores de negocio esperados por fila: se reportan como error de esa fila, no rompen todo.
_ROW_ERRORS = (
    ImportRowError,
    SaleValidationError,
    QuoteValidationError,
    InsufficientStockError,
    ValueError,
    IntegrityError,
)


class _Rollback(Exception):
    """Señal interna para revertir la creación en modo previsualización."""

    def __init__(self, summary):
        self.summary = summary


def run_import(entity, file, user, *, commit, request=None) -> dict:
    handler = get_handler(entity)
    rows = read_entity_sheet(file, handler)  # puede lanzar SheetError (lo maneja la vista)
    records = handler.build_records(rows)
    ctx = handler.build_context(records)

    all_refs = {r.ref for r in records if r.ref}
    existing = handler.existing_refs(all_refs) if all_refs else set()
    # Referencias reservadas del sistema (prefijo SYS-, o coincidencia con un nº de
    # factura/presupuesto ya emitido): reimportar un export no debe re-crear registros.
    reserved = handler.reserved_refs(all_refs) if all_refs else {}
    seen: set[str] = set()

    results = []
    created = 0
    for rec in records:
        entry = {"ref": rec.ref, "rows": rec.row_label, "status": "ok", "detail": "", "errors": []}

        if rec.errors:
            entry["status"] = "error"
            entry["errors"] = rec.errors
            results.append(entry)
            continue

        if rec.ref and (rec.ref in existing or rec.ref in seen):
            entry["status"] = "duplicate"
            entry["detail"] = (
                "Ya fue importada anteriormente (se omite)."
                if rec.ref in existing
                else "Referencia repetida dentro del archivo (se omite)."
            )
            seen.add(rec.ref)
            results.append(entry)
            continue

        if rec.ref in reserved:
            entry["status"] = "error"
            entry["errors"] = [reserved[rec.ref]]
            seen.add(rec.ref)
            results.append(entry)
            continue

        try:
            with transaction.atomic():
                _obj, summary = handler.create(rec, ctx, user)
                if not commit:
                    raise _Rollback(summary)
                entry["status"] = "created"
                entry["detail"] = summary
                created += 1
        except _Rollback as rb:
            entry["status"] = "ok"
            entry["detail"] = rb.summary
        except _ROW_ERRORS as exc:
            entry["status"] = "error"
            entry["errors"] = [str(exc)]
        except Exception as exc:  # noqa: BLE001 — inesperado: no debe tumbar la importación
            entry["status"] = "error"
            entry["errors"] = [f"Error inesperado: {exc}"]

        if rec.ref:
            seen.add(rec.ref)
        results.append(entry)

    summary = {
        "total": len(records),
        "ok": sum(1 for r in results if r["status"] in ("ok", "created")),
        "created": created,
        "errors": sum(1 for r in results if r["status"] == "error"),
        "duplicates": sum(1 for r in results if r["status"] == "duplicate"),
    }

    if commit and created:
        audit.log(
            request=request,
            actor=user,
            action=ActionChoices.DATA_IMPORT,
            category=handler.audit_category,
            description=f"Importó {created} {handler.label.lower()} desde Excel (continuidad operativa).",
            metadata={"entity": entity, **summary},
        )

    return {
        "entity": entity,
        "label": handler.label,
        "committed": commit,
        "summary": summary,
        "records": results,
    }


def export_workbook(entity, params, user, *, request=None):
    handler = get_handler(entity)
    rows = handler.export_rows(params, user)
    bio = build_export_workbook(handler, rows)
    audit.log(
        request=request,
        actor=user,
        action=ActionChoices.DATA_EXPORT,
        category=handler.audit_category,
        description=f"Exportó {len(rows)} fila(s) de {handler.label.lower()} a Excel.",
        metadata={"entity": entity, "rows": len(rows)},
    )
    return bio, len(rows)


def template_workbook(entity):
    return build_template_workbook(get_handler(entity))
