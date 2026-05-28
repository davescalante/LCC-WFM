from django.contrib import admin
from .models import ErlangReport

@admin.register(ErlangReport)
class ErlangReportAdmin(admin.ModelAdmin):
    list_display = ['name', 'calls_per_hour', 'avg_handle_time', 'agents_required', 'service_level_achieved', 'created_at']
