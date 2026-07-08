from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("test_app", "0004_widget"),
    ]

    operations = [
        migrations.CreateModel(
            name="Gadget",
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
                ("qty", models.IntegerField(default=0)),
            ],
        ),
    ]
