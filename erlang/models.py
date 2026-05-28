from django.db import models


class ErlangReport(models.Model):
    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    calls_per_hour = models.FloatField()
    avg_handle_time = models.FloatField(help_text="Average handle time in seconds")
    target_service_level = models.FloatField(help_text="Target service level as percentage (e.g. 80)")
    target_answer_time = models.IntegerField(help_text="Target answer time in seconds (e.g. 20)")
    shrinkage = models.FloatField(default=0, help_text="Shrinkage percentage (e.g. 30 for 30%)")
    agents_required = models.IntegerField(null=True, blank=True)
    agents_scheduled = models.IntegerField(null=True, blank=True, help_text="Agents needed after applying shrinkage")
    service_level_achieved = models.FloatField(null=True, blank=True)
    occupancy = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.created_at.strftime('%Y-%m-%d')}"
