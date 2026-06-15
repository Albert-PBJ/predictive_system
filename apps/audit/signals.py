from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import services
from .models import ActionChoices


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def audit_user_creation(sender, instance, created, **kwargs):
    """Audita la creación de un usuario.

    Los usuarios se crean desde el admin de Django o ``createsuperuser``, donde no hay
    un ``request`` del cual tomar al actor; por eso el actor queda nulo ("sistema"). El
    rol todavía no está definido en este punto (lo asigna otro signal), así que se omite.
    """
    if not created:
        return
    services.log(
        action=ActionChoices.USER_CREATE,
        description=f"Se creó el usuario '{instance.username}'.",
        target=instance,
        target_model="User",
        metadata={
            "username": instance.username,
            "is_superuser": bool(instance.is_superuser),
            "is_staff": bool(instance.is_staff),
        },
    )
