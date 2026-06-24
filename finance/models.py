from decimal import Decimal
from django.db import models
from django.contrib.auth.models import User


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
    default_admin_bonus_mxn = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('500.00'),
        help_text="Default admin bonus in MXN for Official Admins (overridable per-profile)"
    )
    adherence_bonus_max_mxn = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal('400.00'),
        help_text="Maximum adherence bonus in MXN (paid in full when hours >= threshold)"
    )
    adherence_bonus_full_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('40.00'),
        help_text="Hours threshold for full bonus — agents below this receive a proportional amount"
    )
    nr_ratio = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal('0.1250'),
        help_text="NR allowance as a fraction of login+codings time (default 0.125 = 12.5%)"
    )
    nr_ratio_max_hours = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('48.00'),
        help_text="NR ratio deduction only applies when weekly pre-NR hours ≤ this value (default 48)"
    )
    default_tardy_hours = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal('0.25'),
        help_text="Hours assumed lost for a Tardy day when no actual login was recorded (default 0.25 = 15 min)"
    )

    class Meta:
        verbose_name = 'Billing Settings'
        verbose_name_plural = 'Billing Settings'

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def get_for_week(cls, week_start):
        """Return the most-recent BillingSettingsHistory effective on or before week_start,
        falling back to the singleton."""
        history = BillingSettingsHistory.objects.filter(
            week_start__lte=week_start
        ).order_by('-week_start', '-changed_at').first()
        if history:
            return history
        return cls.get()

    def __str__(self):
        return f"Billing Settings (${self.billing_rate_usd}/hr USD, {self.usd_to_mxn} MXN/USD)"


class BillingSettingsHistory(models.Model):
    """Per-week snapshot of BillingSettings — one record per change, keyed by effective week."""
    week_start = models.DateField(db_index=True, help_text="Monday of the first week this rate applies")
    changed_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='billing_settings_changes'
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    billing_rate_usd = models.DecimalField(max_digits=8, decimal_places=2)
    usd_to_mxn = models.DecimalField(max_digits=10, decimal_places=4)
    nr_cap_regular_hours = models.DecimalField(max_digits=5, decimal_places=2)
    nr_cap_kill_team_hours = models.DecimalField(max_digits=5, decimal_places=2)
    default_admin_bonus_mxn = models.DecimalField(max_digits=8, decimal_places=2)
    adherence_bonus_max_mxn = models.DecimalField(max_digits=8, decimal_places=2)
    adherence_bonus_full_hours = models.DecimalField(max_digits=5, decimal_places=2)
    nr_ratio = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal('0.1250'))
    nr_ratio_max_hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('48.00'))
    default_tardy_hours = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal('0.25'))

    class Meta:
        ordering = ['-week_start', '-changed_at']
        verbose_name = 'Billing Settings History'
        verbose_name_plural = 'Billing Settings History'

    @property
    def usd_to_mxn_updated(self):
        return self.changed_at.date() if self.changed_at else None

    def __str__(self):
        return f"Settings effective {self.week_start}"
