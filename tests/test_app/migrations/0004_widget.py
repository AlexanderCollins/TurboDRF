from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("test_app", "0003_brokerage_deal_bankaccount_transaction"),
    ]

    operations = [
        migrations.CreateModel(
            name="Widget",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=100)),
            ],
        ),
    ]
