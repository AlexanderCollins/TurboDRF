# Generated migration for TurboDRF database permissions

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.functions


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TurboDRFRole',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(db_index=True, help_text="Unique role identifier (e.g., 'admin', 'editor', 'viewer')", max_length=100, unique=True)),
                ('description', models.TextField(blank=True, help_text="Human-readable description of this role's purpose")),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True, db_index=True, help_text='Used for cache invalidation')),
                ('version', models.IntegerField(default=1, help_text='Incremented on each update for cache invalidation')),
                ('django_group', models.OneToOneField(blank=True, help_text='Optional link to Django Group for integration', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='turbodrf_role', to='auth.group')),
            ],
            options={
                'verbose_name': 'TurboDRF Role',
                'verbose_name_plural': 'TurboDRF Roles',
                'db_table': 'turbodrf_role',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='UserRole',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('role', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='user_assignments', to='turbodrf.turbodrfrole')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='turbodrf_roles', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'User Role Assignment',
                'verbose_name_plural': 'User Role Assignments',
                'db_table': 'turbodrf_user_role',
            },
        ),
        migrations.CreateModel(
            name='RolePermission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('app_label', models.CharField(db_index=True, help_text="Django app label (e.g., 'books')", max_length=100)),
                ('model_name', models.CharField(db_index=True, help_text="Model name in lowercase (e.g., 'book')", max_length=100)),
                ('action', models.CharField(blank=True, choices=[('read', 'Read'), ('create', 'Create'), ('update', 'Update'), ('delete', 'Delete')], help_text='Model-level action (leave blank for field-level permissions)', max_length=20, null=True)),
                ('field_name', models.CharField(blank=True, db_index=True, help_text='Field name (only for field-level permissions)', max_length=100, null=True)),
                ('permission_type', models.CharField(blank=True, choices=[('read', 'Read'), ('write', 'Write')], help_text='Permission type for field-level permissions', max_length=20, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True, help_text='Used for cache invalidation')),
                ('role', models.ForeignKey(help_text='The role this permission belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='permissions', to='turbodrf.turbodrfrole')),
            ],
            options={
                'verbose_name': 'TurboDRF Permission',
                'verbose_name_plural': 'TurboDRF Permissions',
                'db_table': 'turbodrf_permission',
                'ordering': ['role', 'app_label', 'model_name', 'field_name'],
            },
        ),
        migrations.AddConstraint(
            model_name='userrole',
            constraint=models.UniqueConstraint(fields=('user', 'role'), name='unique_user_role'),
        ),
        migrations.AddConstraint(
            model_name='rolepermission',
            constraint=models.UniqueConstraint(condition=models.Q(('field_name__isnull', True)), fields=('role', 'app_label', 'model_name', 'action'), name='unique_model_permission'),
        ),
        migrations.AddConstraint(
            model_name='rolepermission',
            constraint=models.UniqueConstraint(condition=models.Q(('field_name__isnull', False)), fields=('role', 'app_label', 'model_name', 'field_name', 'permission_type'), name='unique_field_permission'),
        ),
        migrations.AddConstraint(
            model_name='rolepermission',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('action__isnull', False), ('field_name__isnull', True), ('permission_type__isnull', True)), models.Q(('action__isnull', True), ('field_name__isnull', False), ('permission_type__isnull', False)), _connector='OR'), name='permission_type_check'),
        ),
    ]
