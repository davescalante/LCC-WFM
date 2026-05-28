from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0013_shift_template'),
    ]

    operations = [
        migrations.CreateModel(
            name='OvertimeShift',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('start_time', models.TimeField()),
                ('end_time', models.TimeField()),
                ('notes', models.TextField(blank=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='overtime_shifts', to='scheduling.agent')),
            ],
            options={
                'ordering': ['date'],
                'unique_together': {('agent', 'date')},
            },
        ),
    ]
