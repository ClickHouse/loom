"""Members fixture: a Django URL table."""
from django.urls import include, path, re_path

from . import views

urlpatterns = [
    path("articles/<int:year>/", views.year_archive),
    re_path(r"^bio/(?P<username>\w+)/$", views.bio),
    path("api/", include("api.urls")),
]
