from django.urls import path
from . import views

urlpatterns = [
    path('', views.adherence_week, name='adherence_dashboard'),
    path('codings/', views.codings_week, name='codings_week'),
    path('payroll/', views.payroll_export, name='payroll_export'),
    path('daily/', views.daily_hours_week, name='daily_hours'),
    path('save-cell/', views.save_adherence_cell, name='save_adherence_cell'),
    path('save-commission/', views.save_commission, name='save_commission'),
    path('add-coding/', views.add_coding_ajax, name='add_coding_ajax'),
    path('delete-coding/', views.delete_coding_ajax, name='delete_coding_ajax'),
    path('daily/upload/', views.upload_daily_file, name='upload_daily_file'),
    path('daily/delete/', views.delete_daily_upload_ajax, name='delete_daily_upload'),
    path('poll/', views.adherence_poll, name='adherence_poll'),
]
