from django.urls import path

from . import views

app_name = 'nomina'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('inputs/', views.inputs, name='inputs'),
    path('agents/', views.agent_nomina, name='agent_nomina'),
    path('admins/', views.admin_nomina, name='admin_nomina'),
    path('break-abuse/', views.break_abuse, name='break_abuse'),
    path('holidays/', views.holidays, name='holidays'),
    path('loans/', views.loans, name='loans'),
    path('welcome/', views.welcome, name='welcome'),
    path('vacation/', views.vacation, name='vacation'),
    path('overrides/', views.overrides, name='overrides'),
    path('exports/', views.exports, name='exports'),
    path('exports/agent/', views.agent_export, name='agent_export'),
    path('exports/admin/', views.admin_export, name='admin_export'),
]
