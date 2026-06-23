from django.urls import path
from . import views

urlpatterns = [
    path('', views.finance_dashboard, name='finance_dashboard'),
    path('billing/', views.billing_report, name='billing_report'),
    path('billing/export/', views.billing_export, name='billing_export'),
    path('payroll/', views.payroll_report, name='payroll_report'),
    path('payroll/export/', views.payroll_export, name='payroll_export'),
    path('settings/', views.finance_settings, name='finance_settings'),
    path('admin-codings/', views.admin_codings, name='admin_codings'),
    path('add-admin-coding/', views.add_admin_coding_ajax, name='add_admin_coding_ajax'),
    path('edit-admin-coding/', views.edit_admin_coding_ajax, name='edit_admin_coding_ajax'),
    path('delete-admin-coding/', views.delete_admin_coding_ajax, name='delete_admin_coding_ajax'),
    path('admin-adherence/', views.admin_adherence, name='admin_adherence'),
    path('admin-adherence/export/', views.admin_adherence_export, name='admin_adherence_export'),
]
