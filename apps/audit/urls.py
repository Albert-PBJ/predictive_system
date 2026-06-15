"""Rutas de la bitácora de auditoría, montadas bajo ``/api/audit/``."""

from django.urls import path

from .views import (
    AuditLogExportView,
    AuditLogListView,
    AuditLogPurgeView,
    AuditMetaView,
)

urlpatterns = [
    path("logs", AuditLogListView.as_view(), name="audit-logs"),
    path("logs/export", AuditLogExportView.as_view(), name="audit-logs-export"),
    path("logs/purge", AuditLogPurgeView.as_view(), name="audit-logs-purge"),
    path("meta", AuditMetaView.as_view(), name="audit-meta"),
]
