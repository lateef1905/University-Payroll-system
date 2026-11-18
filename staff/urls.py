from rest_framework.routers import DefaultRouter

from .views import DepartmentViewSet, FacultyViewSet, StaffProfileViewSet

router = DefaultRouter()
router.register("faculties", FacultyViewSet, basename="faculty")
router.register("departments", DepartmentViewSet, basename="department")
router.register("profiles", StaffProfileViewSet, basename="staff-profile")

urlpatterns = router.urls
