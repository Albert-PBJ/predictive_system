import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.audit import services as audit
from apps.audit.models import ActionChoices

from .serializers import CustomTokenObtainPairSerializer, UserSerializer

logger = logging.getLogger(__name__)


class LoginView(TokenObtainPairView):
    """POST /api/auth/login — devuelve access, refresh y datos del usuario."""

    serializer_class = CustomTokenObtainPairSerializer
    permission_classes = [AllowAny]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == status.HTTP_200_OK:
            username = request.data.get("username")
            actor = get_user_model().objects.filter(username=username).first()
            audit.log(
                request=request,
                actor=actor,
                action=ActionChoices.LOGIN,
                description=f"Inició sesión el usuario '{username}'.",
                metadata={"username": username},
            )
        return response


class LogoutView(APIView):
    """POST /api/auth/logout — invalida el refresh token (blacklist)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            return Response(
                {"detail": "Se requiere el token de actualización."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            RefreshToken(refresh).blacklist()
        except TokenError:
            return Response(
                {"detail": "Token inválido o ya expirado."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        audit.log(
            request=request,
            action=ActionChoices.LOGOUT,
            description=f"Cerró sesión el usuario '{getattr(request.user, 'username', '')}'.",
        )
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """GET /api/auth/me — datos del usuario autenticado actual."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
