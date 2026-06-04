from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('erlang', '0004_erlangcallrow'),
    ]

    operations = [
        migrations.CreateModel(
            name='ErlangWeekParams',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week_start', models.DateField(unique=True)),
                ('target_sl', models.FloatField(default=80)),
                ('target_seconds', models.IntegerField(default=20)),
                ('shrinkage', models.FloatField(default=0)),
                ('aht_seconds', models.IntegerField(default=420)),
                ('weeks', models.IntegerField(default=3)),
                ('weeks_by_day', models.JSONField(default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['-week_start'],
            },
        ),
    ]
