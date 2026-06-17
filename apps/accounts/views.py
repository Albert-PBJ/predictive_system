import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.audit import services as audit
from apps.audit.models import ActionChoices

from .models import UserProfile
from .serializers import (
    CustomTokenObtainPairSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)

# Mensajes en español para los validadores de contraseña de Django (el proyecto
# corre con LANGUAGE_CODE="en-us", así que sus mensajes vienen en inglés). Se mapean
# por código de error para mantener las respuestas de la API en español.
_PASSWORD_ERRORS_ES = {
    "password_too_short": "La contraseña es demasiado corta. Debe tener al menos {min_length} caracteres.",
    "password_too_common": "La contraseña es demasiado común; elige una menos predecible.",
    "password_entirely_numeric": "La contraseña no puede ser completamente numérica.",
    "password_too_similar": "La contraseña es demasiado parecida a tus datos personales.",
}


def _password_errors_es(exc):
    """Traduce a español los errores de ``validate_password`` (por código de error)."""
    out = []
    for err in exc.error_list:
        code = getattr(err, "code", "") or ""
        params = getattr(err, "params", None) or {}
        template = _PASSWORD_ERRORS_ES.get(code)
        if template:
            try:
                out.append(template.format(**params))
            except (KeyError, IndexError):
                out.append(template)
        else:
            out.extend(err.messages)
    return out


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


class PasswordResetRequestView(APIView):
    """POST /api/auth/password-reset — inicia la recuperación de contraseña por correo.

    Por seguridad responde **siempre 200** con un mensaje genérico, exista o no el correo,
    para no revelar qué cuentas están registradas. El correo es la fuente de verdad en el
    perfil (``UserProfile.email``), no en ``auth_user``. Queda registrado en la auditoría.
    """

    permission_classes = [AllowAny]
    throttle_scope = "password_reset"

    _GENERIC = {
        "detail": "Si el correo está registrado, te enviamos un enlace para restablecer la contraseña."
    }

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip()

        profile = (
            UserProfile.objects.select_related("user")
            .filter(email__iexact=email, user__is_active=True)
            .first()
        )
        if profile and profile.email:
            try:
                self._send_reset_email(profile)
            except Exception as exc:  # noqa: BLE001 — no romper el flujo ni revelar el fallo
                logger.warning("No se pudo enviar el correo de restablecimiento: %s", exc)
            else:
                audit.log(
                    request=request,
                    actor=profile.user,
                    action=ActionChoices.PASSWORD_RESET_REQUEST,
                    description=f"Solicitó recuperar la contraseña del usuario '{profile.user.username}'.",
                    metadata={"email": profile.email},
                )
        return Response(self._GENERIC, status=status.HTTP_200_OK)

    @staticmethod
    def _send_reset_email(profile):
        user = profile.user
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        base = settings.FRONTEND_BASE_URL.rstrip("/")
        link = f"{base}/restablecer-contrasena?uid={uid}&token={token}"
        hours = max(1, settings.PASSWORD_RESET_TIMEOUT // 3600)
        subject = "Restablece tu contraseña — Inversiones Maescar"
        message = (
            f"Hola {profile.full_name},\n\n"
            f"Recibimos una solicitud para restablecer la contraseña de tu cuenta "
            f"'{user.username}' en el sistema de Inversiones Maescar C.A.\n\n"
            f"Para crear una nueva contraseña, abre este enlace:\n\n{link}\n\n"
            f"El enlace vence en {hours} hora(s). Si no solicitaste el cambio, ignora "
            f"este correo: tu contraseña seguirá igual.\n\n"
            f"— Inversiones Maescar C.A."
        )
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [profile.email], fail_silently=False)
        logger.info("Correo de restablecimiento enviado al usuario '%s'.", user.username)


class PasswordResetConfirmView(APIView):
    """POST /api/auth/password-reset/confirm — fija la nueva contraseña dado uid + token.

    Valida el token (de un solo uso y con vencimiento) y la fortaleza de la nueva
    contraseña. Al cambiarla, invalida las sesiones activas (blacklist de refresh tokens)
    y registra el hecho en la auditoría.
    """

    permission_classes = [AllowAny]
    throttle_scope = "password_reset_confirm"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = self._user_from_uid(data["uid"])
        if user is None or not default_token_generator.check_token(user, data["token"]):
            return Response(
                {"detail": "El enlace es inválido o ya expiró. Solicita uno nuevo."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_password(data["new_password"], user=user)
        except DjangoValidationError as exc:
            return Response(
                {"new_password": _password_errors_es(exc)}, status=status.HTTP_400_BAD_REQUEST
            )

        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        self._revoke_sessions(user)
        audit.log(
            request=request,
            actor=user,
            action=ActionChoices.PASSWORD_RESET,
            description=f"Restableció su contraseña el usuario '{user.username}'.",
        )
        return Response(
            {"detail": "Tu contraseña se actualizó. Ya puedes iniciar sesión."},
            status=status.HTTP_200_OK,
        )

    @staticmethod
    def _user_from_uid(uidb64):
        User = get_user_model()
        try:
            uid = force_str(urlsafe_base64_decode(uidb64))
            return User.objects.get(pk=uid, is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return None

    @staticmethod
    def _revoke_sessions(user):
        """Invalida los refresh tokens vigentes tras el cambio (mejor práctica de seguridad)."""
        try:
            for token in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=token)
        except Exception as exc:  # noqa: BLE001 — la contraseña ya cambió; no debe fallar la respuesta
            logger.warning("No se pudieron invalidar las sesiones del usuario '%s': %s", user.username, exc)
