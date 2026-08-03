from django.urls import path

from . import views

app_name = 'nomina'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('inputs/', views.inputs, name='inputs'),
    path('agents/', views.agent_nomina, name='agent_nomina'),
    path('admins/', views.admin_nomina, name='admin_nomina'),
]
