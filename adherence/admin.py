from django.contrib import admin
from .models import AdherenceRecord, Coding, PayrollAdjustment


@admin.register(AdherenceRecord)
class AdherenceRecordAdmin(admin.ModelAdmin):
    list_display = ['agent', 'date', 'status', 'actual_hours', 'updated_at']
    list_filter = ['status', 'date']
    search_fields = ['agent__agent_name', 'agent__user__first_name', 'agent__user__last_name']


@admin.register(Coding)
class CodingAdmin(admin.ModelAdmin):
    list_display = ['agent', 'date', 'start_time', 'end_time', 'notes', 'created_at']
    list_filter = ['date']
    search_fields = ['agent__agent_name', 'agent__user__first_name', 'agent__user__last_name']


@admin.register(PayrollAdjustment)
class PayrollAdjustmentAdmin(admin.ModelAdmin):
    list_display = ['agent', 'week_start', 'commission_deduction', 'notes']
    list_filter = ['week_start']
