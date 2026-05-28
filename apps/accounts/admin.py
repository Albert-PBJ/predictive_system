from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = "Perfil"
    fields = ("role", "first_name", "last_name", "email", "phone")


class UserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ("username", "get_email", "get_full_name_display", "get_role", "is_active")

    # auth_user se usa solo para autenticación: ocultamos los datos personales
    # del formulario (viven en el perfil) y dejamos credenciales/permisos/fechas.
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Permisos", {
            "fields": (
                "is_active",
                "is_staff",
                "is_superuser",
                "groups",
                "user_permissions",
            ),
        }),
        ("Fechas importantes", {"fields": ("last_login", "date_joined")}),
    )

    @admin.display(description="Rol")
    def get_role(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.get_role_display() if profile else "—"

    @admin.display(description="Correo")
    def get_email(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.email if profile else "—"

    @admin.display(description="Nombre")
    def get_full_name_display(self, obj):
        profile = getattr(obj, "profile", None)
        return profile.full_name if profile else obj.username


admin.site.unregister(User)
admin.site.register(User, UserAdmin)
