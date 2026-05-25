from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("products", "0002_alter_product_table"),
    ]

    operations = [
        migrations.DeleteModel(name="Product"),
    ]
