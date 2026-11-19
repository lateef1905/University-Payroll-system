from django.urls import path

from .views import AuditEventListView, ChangeHistoryView

urlpatterns = [
    path("changes/", ChangeHistoryView.as_view(), name="audit-changes"),
    path("events/", AuditEventListView.as_view(), name="audit-events"),
]
