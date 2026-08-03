from decimal import Decimal

from django.db import models


class NominaWeek(models.Model):
    """Per-week Nómina settings. The spiff USD→MXN rate must be entered fresh
    each week — it starts empty (no default/carry-over)."""
    week_start = models.DateField(unique=True)
    spiff_fx_rate = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="USD→MXN rate for spiffs — set each week (starts empty)",
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


class Loan(models.Model):
    """An agent loan (préstamo). 1-week term charges ×1.25, 2-week ×1.35. The
    total owed is repaid in equal weekly installments starting from start_week;
    each covered week deducts one installment from that agent's nómina."""
    TERM_CHOICES = [(1, '1 week (×1.25)'), (2, '2 weeks (×1.35)')]
    agent = models.ForeignKey('scheduling.Agent', on_delete=models.CASCADE, related_name='nomina_loans')
    principal = models.DecimalField(max_digits=10, decimal_places=2)
    term_weeks = models.PositiveSmallIntegerField(choices=TERM_CHOICES, default=1)
    rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('1.25'))
    start_week = models.DateField(help_text="Monday of the first repayment week")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def total_owed(self):
        return (self.principal * self.rate).quantize(Decimal('0.01'))

    @property
    def weekly_repayment(self):
        return (self.total_owed / self.term_weeks).quantize(Decimal('0.01'))

    def installment_for_week(self, week_start):
        """The repayment to deduct for the given pay week (0 if not in range)."""
        from datetime import timedelta
        for i in range(self.term_weeks):
            if self.start_week + timedelta(days=7 * i) == week_start:
                return self.weekly_repayment
        return Decimal('0')

    def weeks_elapsed(self, as_of_week):
        from datetime import timedelta
        n = 0
        for i in range(self.term_weeks):
            if self.start_week + timedelta(days=7 * i) <= as_of_week:
                n += 1
        return n

    def balance(self, as_of_week):
        return (self.total_owed - self.weekly_repayment * self.weeks_elapsed(as_of_week)).quantize(Decimal('0.01'))

    def __str__(self):
        return f"{self.agent} — ${self.total_owed} ({self.term_weeks}wk)"


class WelcomeBonusEnrollment(models.Model):
    """Welcome-bonus eligibility. For each covered week the agent is paid
    `amount` IF they earned an adherence bonus that week. Every calendar week
    counts toward `num_weeks`; after that the enrollment lapses."""
    agent = models.ForeignKey('scheduling.Agent', on_delete=models.CASCADE, related_name='nomina_welcome')
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1000'))
    num_weeks = models.PositiveSmallIntegerField(default=4)
    start_week = models.DateField(help_text="Monday of the first eligible week")

    def covers_week(self, week_start):
        from datetime import timedelta
        return self.start_week <= week_start < self.start_week + timedelta(days=7 * self.num_weeks)

    def __str__(self):
        return f"{self.agent} — welcome ${self.amount}×{self.num_weeks}"


class NominaOverride(models.Model):
    """A manual override of a single computed cell for one agent/week. When set,
    the override value replaces the computed value in the nómina (super-admin
    'override anything')."""
    agent = models.ForeignKey('scheduling.Agent', on_delete=models.CASCADE, related_name='nomina_overrides')
    week_start = models.DateField()
    field = models.CharField(max_length=40)
    value = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        unique_together = ('agent', 'week_start', 'field')

    def __str__(self):
        return f"{self.agent} — {self.week_start} — {self.field}={self.value}"


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
