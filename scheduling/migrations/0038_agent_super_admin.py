from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0037_agent_official_admin_bonus'),
    ]

    operations = [
        migrations.AddField(
            model_name='agent',
            name='is_super_admin',
            field=models.BooleanField(default=False, help_text='Super admin — full access to Finance section; can grant super admin to others'),
        ),
    ]
