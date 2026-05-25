from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("competitor_market_data", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="CompetitorMarketData"),
    ]
