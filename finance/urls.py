from django.urls import path
from . import views

urlpatterns = [
    path('', views.finance_dashboard, name='finance_dashboard'),
    path('billing/', views.billing_report, name='billing_report'),
    path('billing/export/', views.billing_export, name='billing_export'),
    path('billing/v2/export/', views.billing_export_v2, name='billing_export_v2'),
    path('payroll/', views.payroll_report, name='payroll_report'),
    path('payroll/export/', views.payroll_export, name='payroll_export'),
    path('settings/', views.finance_settings, name='finance_settings'),
    path('codings/export/', views.codings_export, name='codings_export'),
    path('adherence/export/', views.adherence_export, name='adherence_export'),
    path('user-audit/export/', views.user_audit_export, name='user_audit_export'),
]
