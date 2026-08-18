from decimal import Decimal

from django.db import migrations

# One-off business config: Erick (erickv) gets a higher individual adherence-bonus
# cap of 1,000 MXN instead of the global default (currently 400). The proration rule
# is unchanged — below the full-hours threshold he earns the same fraction of 1,000.
# Idempotent and a no-op if the username is absent (e.g. fresh/test databases).
ERICK_USERNAME = "erickv"
ERICK_CAP_MXN = Decimal("1000.00")


def set_erick_cap(apps, schema_editor):
    Agent = apps.get_model("scheduling", "Agent")
    Agent.objects.filter(user__username=ERICK_USERNAME).update(
        adherence_bonus_max_mxn=ERICK_CAP_MXN
    )


def unset_erick_cap(apps, schema_editor):
    Agent = apps.get_model("scheduling", "Agent")
    Agent.objects.filter(user__username=ERICK_USERNAME).update(
        adherence_bonus_max_mxn=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0050_agent_adherence_bonus_max_mxn"),
    ]

    operations = [
        migrations.RunPython(set_erick_cap, unset_erick_cap),
    ]
