from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('adherence', '0008_add_performance_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='coding',
            name='is_admin_coding',
            field=models.BooleanField(default=False, help_text='Admin-only coding (Finance section); excluded from regular Codings/Adherence tabs'),
        ),
    ]
