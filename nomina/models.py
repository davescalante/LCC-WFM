from decimal import Decimal

from django.db import models


class NominaWeek(models.Model):
    """Per-week Nómina settings (e.g. the spiff USD→MXN rate the user sets each week)."""
    week_start = models.DateField(unique=True)
    spiff_fx_rate = models.DecimalField(
        max_digits=8, decimal_places=4, default=Decimal('17.41'),
        help_text="USD→MXN rate used to convert spiffs this week",
    )

    def __str__(self):
        return f"Nómina week {self.week_start}"


class WeeklyPayInput(models.Model):
    """The manual paste-in amounts for one agent for one week — the columns the
    app doesn't compute (from external reports the user pastes in). Every value
    is editable/overridable. Mirrors the PayrollAdjustment per-agent/per-week pattern."""
    agent = models.ForeignKey('scheduling.Agent', on_delete=models.CASCADE, related_name='nomina_inputs')
    week_start = models.DateField()

    lpo = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Sales commission (MXN)")
    spiff_usd = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Spiffs in USD (converted at the week's rate)")
    welcome = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    referral = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    kill_team_qa = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    comedor = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Cafeteria charges — deducted")
    transportation = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Transportation — deducted")

    class Meta:
        unique_together = ('agent', 'week_start')

    def __str__(self):
        return f"{self.agent} — {self.week_start}"


class Holiday(models.Model):
    """A company-wide holiday date. An agent who WORKS a designated holiday earns
    a 2× premium on those hours (on top of the 1× already in their pay = triple)."""
    date = models.DateField(unique=True)
    name = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.date} — {self.name}" if self.name else str(self.date)


class BreakAbuseIncident(models.Model):
    """A logged break-abuse incident. Any incident in a pay week zeroes that
    agent's adherence bonus for the week."""
    agent = models.ForeignKey('scheduling.Agent', on_delete=models.CASCADE, related_name='break_abuse_incidents')
    date = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    logged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.agent} — break abuse {self.date}"
