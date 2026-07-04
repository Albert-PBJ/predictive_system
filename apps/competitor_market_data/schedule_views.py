"""API REST de las programaciones de scraping automático (`ScraperSchedule`).

CRUD + consulta de "vencidas" + marcado de "ejecutada". Todo bajo ADMIN. El disparo
real de la corrida lo hace el frontend (el procesamiento es dirigido por el navegador):
al iniciar sesión el admin, consulta las vencidas (`due`), lanza el scraping reutilizando
el flujo por lotes y marca cada una como ejecutada (`ran`), avanzando su `next_run_at`.
"""

from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdmin

from .models import ScraperSchedule


class ScraperScheduleSerializer(serializers.ModelSerializer):
    """Serializa una programación; expone etiquetas legibles y si está vencida."""

    source_display = serializers.CharField(source="get_source_display", read_only=True)
    frequency_display = serializers.CharField(source="get_frequency_display", read_only=True)
    is_due = serializers.BooleanField(read_only=True)

    class Meta:
        model = ScraperSchedule
        fields = [
            "id", "name", "source", "urls", "competitor_name", "limit", "frequency",
            "is_active", "last_run_at", "next_run_at", "created_at", "updated_at",
            "source_display", "frequency_display", "is_due",
        ]
        read_only_fields = ["last_run_at", "created_at", "updated_at"]
        extra_kwargs = {
            # next_run_at es opcional al crear: si se omite, se corre en la próxima sesión.
            "next_run_at": {"required": False},
        }

    def validate_urls(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Debe ser una lista de URLs/términos.")
        cleaned = [str(u).strip() for u in value if str(u).strip()]
        if not cleaned:
            raise serializers.ValidationError("Agrega al menos una URL o término de búsqueda.")
        return cleaned

    def validate_source(self, value):
        if value not in ScraperSchedule.SourceChoices.values:
            raise serializers.ValidationError("Fuente de scraping no válida.")
        return value

    def create(self, validated_data):
        # Si no se indica cuándo, la primera corrida queda vencida ya (próxima sesión).
        validated_data.setdefault("next_run_at", timezone.now())
        return super().create(validated_data)


class ScraperScheduleListCreateView(ListCreateAPIView):
    """GET (lista completa) / POST (crear) de programaciones. ADMIN."""

    permission_classes = [IsAdmin]
    serializer_class = ScraperScheduleSerializer
    queryset = ScraperSchedule.objects.all()
    pagination_class = None  # lista corta: sin paginación


class ScraperScheduleDetailView(RetrieveUpdateDestroyAPIView):
    """GET / PATCH / DELETE de una programación por id. ADMIN."""

    permission_classes = [IsAdmin]
    serializer_class = ScraperScheduleSerializer
    queryset = ScraperSchedule.objects.all()


class ScraperScheduleDueView(APIView):
    """GET /scrapers/schedules/due → programaciones activas ya vencidas (para disparar)."""

    permission_classes = [IsAdmin]

    def get(self, request: Request) -> Response:
        now = timezone.now()
        due = ScraperSchedule.objects.filter(is_active=True, next_run_at__lte=now)
        return Response(
            {"due": ScraperScheduleSerializer(due, many=True).data},
            status=status.HTTP_200_OK,
        )


class ScraperScheduleRanView(APIView):
    """POST /scrapers/schedules/<pk>/ran → marca ejecutada y avanza `next_run_at`.

    El frontend la llama al lanzar la corrida (automática o manual "Ejecutar ahora"),
    de forma que una programación vencida no se vuelva a disparar en la misma ventana.
    """

    permission_classes = [IsAdmin]

    def post(self, request: Request, pk: int) -> Response:
        schedule = ScraperSchedule.objects.filter(pk=pk).first()
        if schedule is None:
            return Response(
                {"error": "Programación no encontrada."}, status=status.HTTP_404_NOT_FOUND
            )
        schedule.mark_ran()
        return Response(ScraperScheduleSerializer(schedule).data, status=status.HTTP_200_OK)
