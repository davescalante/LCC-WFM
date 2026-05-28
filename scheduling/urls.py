from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('agents/', views.agent_list, name='agent_list'),
    path('agents/add/', views.agent_create, name='agent_create'),
    path('agents/<int:pk>/', views.agent_detail, name='agent_detail'),
    path('agents/<int:pk>/edit/', views.agent_edit, name='agent_edit'),
    path('agents/<int:pk>/delete/', views.agent_delete, name='agent_delete'),
    path('shifts/', views.shift_list, name='shift_list'),
    path('shifts/week/', views.shift_week, name='shift_week'),
    path('shifts/<int:pk>/edit/', views.shift_edit, name='shift_edit'),
    path('shifts/<int:pk>/delete/', views.shift_delete, name='shift_delete'),
]
