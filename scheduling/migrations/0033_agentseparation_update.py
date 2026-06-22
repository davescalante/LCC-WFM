from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('scheduling', '0032_agentseparation'),
    ]

    operations = [
        # 1. Add status field
        migrations.AddField(
            model_name='agentseparation',
            name='status',
            field=models.CharField(
                choices=[
                    ('in_progress', 'In Progress'),
                    ('finalized', 'Finalized'),
                    ('cancelled', 'Cancelled'),
                ],
                default='finalized',
                max_length=20,
            ),
        ),
        # 2. Rename separation_date → remove_from_adherence_date
        migrations.RenameField(
            model_name='agentseparation',
            old_name='separation_date',
            new_name='remove_from_adherence_date',
        ),
        # 3. Alter remove_from_adherence_date to be nullable
        migrations.AlterField(
            model_name='agentseparation',
            name='remove_from_adherence_date',
            field=models.DateField(
                blank=True,
                null=True,
                help_text='Monday of first week agent no longer appears on Adherence',
            ),
        ),
        # 4. Add finalized_by ForeignKey
        migrations.AddField(
            model_name='agentseparation',
            name='finalized_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='separations_finalized',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # 5. Add finalized_at DateTimeField
        migrations.AddField(
            model_name='agentseparation',
            name='finalized_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        # 6. Alter agent field from OneToOneField to ForeignKey (removes unique constraint)
        migrations.AlterField(
            model_name='agentseparation',
            name='agent',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='separations',
                to='scheduling.agent',
            ),
        ),
    ]
