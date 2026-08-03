from django.urls import path

from . import views

app_name = 'nomina'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('agents/', views.agent_nomina, name='agent_nomina'),
]
