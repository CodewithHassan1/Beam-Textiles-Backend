from rest_framework.permissions import BasePermission, SAFE_METHODS
from .models import Profile


def user_role(user):
    """Resolve a user's role, defaulting superusers to super_admin and others to staff."""
    if not user or not user.is_authenticated:
        return None
    prof = getattr(user, 'profile', None)
    if prof:
        return prof.role
    return Profile.ROLE_SUPER_ADMIN if user.is_superuser else Profile.ROLE_STAFF


def user_rank(user):
    return Profile.RANK.get(user_role(user), 0)


class RoleBasedPermission(BasePermission):
    """
    Accounting-safe RBAC for the ERP resource viewsets:
      - Read (GET/HEAD/OPTIONS): any authenticated user (Staff and above)
      - Create / Update (POST/PUT/PATCH): Manager and above
      - Delete (DELETE): Admin and above

    Super Admin and Admin therefore retain full CRUD, so existing admin-driven
    clients are unaffected.
    """
    WRITE_MIN_RANK = Profile.RANK[Profile.ROLE_MANAGER]
    DELETE_MIN_RANK = Profile.RANK[Profile.ROLE_ADMIN]

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        if request.method == 'DELETE':
            return user_rank(request.user) >= self.DELETE_MIN_RANK
        return user_rank(request.user) >= self.WRITE_MIN_RANK


class IsAdminRole(BasePermission):
    """Admin or Super Admin only (e.g. audit-log access, user management)."""
    def has_permission(self, request, view):
        return user_rank(request.user) >= Profile.RANK[Profile.ROLE_ADMIN]
