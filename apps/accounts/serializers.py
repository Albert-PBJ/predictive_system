from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Role, UserProfile

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    """Datos públicos del usuario autenticado (no expone campos sensibles).

    Los datos personales (nombre, apellido, correo) provienen del perfil,
    que es la fuente de verdad; auth_user se mantiene solo para autenticación.
    """

    email = serializers.EmailField(source="profile.email", read_only=True)
    first_name = serializers.CharField(source="profile.first_name", read_only=True)
    last_name = serializers.CharField(source="profile.last_name", read_only=True)
    phone = serializers.CharField(source="profile.phone", read_only=True)
    role = serializers.CharField(source="profile.role", read_only=True)
    role_display = serializers.CharField(source="profile.get_role_display", read_only=True)
    full_name = serializers.CharField(source="profile.full_name", read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "phone",
            "role",
            "role_display",
            "is_staff",
            "is_superuser",
        )
        read_only_fields = fields


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Incluye el rol y datos básicos del usuario en el token y en la respuesta."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        profile = getattr(user, "profile", None)
        token["role"] = profile.role if profile else Role.VIEWER
        token["username"] = user.username
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data
