from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0027_scheduledrolechange'),
    ]

    operations = [
        migrations.AddField(
            model_name='scheduledrolechange',
            name='new_supervisor',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='+',
                to='scheduling.agent',
            ),
        ),
    ]
