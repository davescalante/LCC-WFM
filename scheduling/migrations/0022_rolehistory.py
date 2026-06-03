from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0021_shifttemplate_remove_unique_together'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RoleHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(max_length=20)),
                ('role_type', models.CharField(blank=True, max_length=20)),
                ('employer', models.CharField(blank=True, max_length=20)),
                ('billing_status', models.CharField(blank=True, max_length=20)),
                ('effective_from', models.DateField()),
                ('effective_to', models.DateField(blank=True, null=True)),
                ('changed_at', models.DateTimeField(auto_now_add=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='role_history', to='scheduling.agent')),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('supervisor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='scheduling.agent')),
            ],
            options={
                'ordering': ['-effective_from', '-changed_at'],
            },
        ),
    ]
