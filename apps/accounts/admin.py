from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.forms.models import BaseInlineFormSet

from .models import UserProfile

User = get_user_model()


class UserProfileInlineFormSet(BaseInlineFormSet):
    """Evita duplicar el perfil al crear un usuario desde el admin.

    El perfil lo crea el signal `post_save` del User (ver signals.py). Al dar de
    alta un usuario, el inline se construye ANTES de que el usuario exista, así
    que llega como formulario "nuevo" e intenta INSERTAR un segundo perfil sobre
    el mismo usuario (`user` es OneToOne) -> "duplicate key value violates unique
    constraint user_profiles_user_id_key". Aquí redirigimos ese INSERT sobre el
    perfil que el signal acaba de crear, de modo que sea un UPDATE.
    """

    def save_new(self, form, commit=True):
        profile = super().save_new(form, commit=False)
        existing = UserProfile.objects.filter(user=self.instance).first()
        if existing is not None:
            profile.pk = existing.pk
            # created_at es auto_now_add: en un UPDATE no se recalcula, así que
            # conservamos el valor original en lugar de escribir NULL.
            profile.created_at = existing.created_at
            profile._state.adding = False
        if commit:
            profile.save()
        return profile


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    formset = UserProfileInlineFormSet
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
