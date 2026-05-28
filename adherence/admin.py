from django.contrib import admin
from .models import AdherenceRecord, Coding, PayrollAdjustment, DailyUpload, DailyAgentHours


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


@admin.register(DailyUpload)
class DailyUploadAdmin(admin.ModelAdmin):
    list_display = ['date', 'filename', 'row_count', 'unmatched_count', 'uploaded_at']
    list_filter = ['date']


@admin.register(DailyAgentHours)
class DailyAgentHoursAdmin(admin.ModelAdmin):
    list_display = ['five9_username', 'agent', 'upload', 'login_seconds', 'not_ready_seconds']
    list_filter = ['upload__date']
    search_fields = ['five9_username', 'agent__agent_name']
