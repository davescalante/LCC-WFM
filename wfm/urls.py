from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('scheduling/', include('scheduling.urls')),
    path('adherence/', include('adherence.urls')),
    path('erlang/', include('erlang.urls')),
    path('', include('scheduling.urls')),
]
