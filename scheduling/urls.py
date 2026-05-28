from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('agents/', views.agent_list, name='agent_list'),
    path('agents/<int:pk>/', views.agent_detail, name='agent_detail'),
    path('shifts/', views.shift_list, name='shift_list'),
]
