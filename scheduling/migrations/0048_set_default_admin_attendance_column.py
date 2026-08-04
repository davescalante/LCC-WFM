from django.db import migrations


def set_default_if_column_exists(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return  # no-op on SQLite (local dev, fresh test DBs)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'scheduling_agent'
              AND column_name = 'can_access_admin_attendance'
            """
        )
        if cursor.fetchone():
            cursor.execute(
                'ALTER TABLE "scheduling_agent" '
                'ALTER COLUMN "can_access_admin_attendance" SET DEFAULT false'
            )


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0047_agent_can_manage_loans"),
    ]

    operations = [
        migrations.RunPython(
            set_default_if_column_exists,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
