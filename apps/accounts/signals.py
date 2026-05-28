from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Role, UserProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_user_profile(sender, instance, created, **kwargs):
    """Crea el perfil al crear el usuario.

    Los superusuarios reciben el rol ADMIN; el resto, el rol por defecto (VIEWER).
    Un administrador puede ajustar el rol después desde el admin de Django.
    """
    if created or not hasattr(instance, "profile"):
        role = Role.ADMIN if instance.is_superuser else Role.VIEWER
        UserProfile.objects.get_or_create(
            user=instance,
            defaults={
                "role": role,
                # Migra los datos personales que Django pide al crear el usuario
                # (ej. createsuperuser) hacia el perfil, que es la fuente de verdad.
                "first_name": instance.first_name,
                "last_name": instance.last_name,
                "email": instance.email,
            },
        )
