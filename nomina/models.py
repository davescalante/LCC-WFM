from django.db import models


class Holiday(models.Model):
    """A company-wide holiday date.

    Used by the Nómina engine (later phases): an agent who WORKS a designated
    holiday earns triple pay on those hours; a scheduled agent marked 'Holiday'
    who does not work is paid their scheduled hours at the standard rate. This
    model only records which dates are holidays — no pay logic lives here yet.
    """
    date = models.DateField(unique=True)
    name = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['-date']

    def __str__(self):
        return f"{self.date} — {self.name}" if self.name else str(self.date)
