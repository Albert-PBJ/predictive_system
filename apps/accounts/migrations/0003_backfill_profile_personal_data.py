from django.db import migrations


def copy_personal_data_to_profile(apps, schema_editor):
    """Mueve nombre, apellido y correo de auth_user al perfil existente."""
    UserProfile = apps.get_model("accounts", "UserProfile")
    for profile in UserProfile.objects.select_related("user").all():
        user = profile.user
        if not user:
            continue
        profile.first_name = user.first_name
        profile.last_name = user.last_name
        profile.email = user.email
        profile.save(update_fields=["first_name", "last_name", "email"])


def noop_reverse(apps, schema_editor):
    # Los datos siguen existiendo en auth_user; no hay nada que revertir.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_userprofile_email_userprofile_first_name_and_more"),
    ]

    operations = [
        migrations.RunPython(copy_personal_data_to_profile, noop_reverse),
    ]
