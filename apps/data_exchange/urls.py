from django.urls import path

from . import views

# Montado en /api/data-exchange/. <entity> ∈ sales|inventory|customers|quotes.
urlpatterns = [
    path("<str:entity>/template", views.TemplateView.as_view()),
    path("<str:entity>/export", views.ExportView.as_view()),
    path("<str:entity>/preview", views.ImportPreviewView.as_view()),
    path("<str:entity>/import", views.ImportCommitView.as_view()),
]
