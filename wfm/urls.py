from django.contrib import admin
from django.urls import path, include
from finance import views as finance_views
from nomina import views as nomina_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('scheduling/', include('scheduling.urls')),
    path('adherence/', include('adherence.urls')),
    path('erlang/', include('erlang.urls')),
    path('finance/', include('finance.urls')),
    path('nomina/', include('nomina.urls')),
    path('vacations/', nomina_views.vacations, name='vacations'),
    path('admin-codings/', finance_views.admin_codings, name='admin_codings'),
    path('admin-codings/add/', finance_views.add_admin_coding_ajax, name='add_admin_coding_ajax'),
    path('admin-codings/edit/', finance_views.edit_admin_coding_ajax, name='edit_admin_coding_ajax'),
    path('admin-codings/delete/', finance_views.delete_admin_coding_ajax, name='delete_admin_coding_ajax'),
    path('admin-adherence/', finance_views.admin_adherence, name='admin_adherence'),
    path('admin-adherence/export/', finance_views.admin_adherence_export, name='admin_adherence_export'),
    path('admin-adherence/penalty-reco/', finance_views.admin_penalty_reco, name='admin_penalty_reco'),
    path('admin-adherence/save-deduction/', finance_views.save_admin_deduction, name='save_admin_deduction'),
    path('', include('scheduling.urls')),
]
