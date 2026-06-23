from decimal import Decimal
from django.db import models


class BillingSettings(models.Model):
    """Singleton — always use BillingSettings.get() to access."""
    billing_rate_usd = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('15.00'),
        help_text="Infinity billing rate to LCC (USD per hour, applies to all billable employees)"
    )
    usd_to_mxn = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('17.0000'),
        help_text="USD to MXN conversion rate"
    )
    usd_to_mxn_updated = models.DateField(
        null=True, blank=True,
        help_text="Date the exchange rate was last updated"
    )
    nr_cap_regular_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('6.00'),
        help_text="Weekly not-ready cap for Regular Agents (hours)"
    )
    nr_cap_kill_team_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('7.00'),
        help_text="Weekly not-ready cap for Kill Team agents (hours)"
    )
    adherence_bonus_max_mxn = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('400.00'),
        help_text="Maximum adherence bonus in MXN (paid in full when hours >= threshold)"
    )
    adherence_bonus_full_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('40.00'),
        help_text="Hours threshold for full bonus — agents below this receive a proportional amount"
    )

    class Meta:
        verbose_name = 'Billing Settings'
        verbose_name_plural = 'Billing Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Billing Settings (${self.billing_rate_usd}/hr USD, {self.usd_to_mxn} MXN/USD)"
