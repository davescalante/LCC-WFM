from django.contrib import admin
from .models import AdherenceRecord

@admin.register(AdherenceRecord)
class AdherenceRecordAdmin(admin.ModelAdmin):
    list_display = ['shift', 'status', 'actual_start', 'actual_end', 'timestamp']
    list_filter = ['status', 'shift__date']
