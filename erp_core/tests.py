from django.test import override_settings
from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from rest_framework.authtoken.models import Token

from .models import Profile, ActivityLog, Account

# Disable throttling during tests so repeated logins don't hit the rate limit.
NO_THROTTLE = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework.authentication.TokenAuthentication'],
    'DEFAULT_PERMISSION_CLASSES': ['rest_framework.permissions.IsAuthenticated'],
    'DEFAULT_RENDERER_CLASSES': ['rest_framework.renderers.JSONRenderer'],
}


def make_user(username, role, password='Str0ngPass!2026'):
    user = User.objects.create_user(username=username, password=password)
    user.profile.role = role
    user.profile.save()
    token, _ = Token.objects.get_or_create(user=user)
    return user, token.key


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class AuthTests(APITestCase):
    def test_signup_rejects_weak_password(self):
        res = self.client.post('/api/auth/signup/', {'username': 'weaky', 'password': '123'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertFalse(User.objects.filter(username='weaky').exists())

    def test_signup_strong_password_defaults_to_staff(self):
        res = self.client.post('/api/auth/signup/', {'username': 'newbie', 'password': 'Str0ngPass!2026'}, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['role'], Profile.ROLE_STAFF)

    def test_login_returns_role_and_writes_audit(self):
        make_user('alice', Profile.ROLE_MANAGER)
        res = self.client.post('/api/auth/login/', {'username': 'alice', 'password': 'Str0ngPass!2026'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['role'], Profile.ROLE_MANAGER)
        self.assertTrue(ActivityLog.objects.filter(action='login', username='alice').exists())

    def test_account_lockout_after_repeated_failures(self):
        User.objects.create_user(username='bob', password='Str0ngPass!2026')
        for _ in range(5):
            self.client.post('/api/auth/login/', {'username': 'bob', 'password': 'wrong'}, format='json')
        res = self.client.post('/api/auth/login/', {'username': 'bob', 'password': 'Str0ngPass!2026'}, format='json')
        self.assertEqual(res.status_code, 403)  # locked even though password is now correct


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class RBACTests(APITestCase):
    def setUp(self):
        _, self.staff = make_user('staff1', Profile.ROLE_STAFF)
        _, self.manager = make_user('manager1', Profile.ROLE_MANAGER)
        _, self.admin = make_user('admin1', Profile.ROLE_ADMIN)

    def auth(self, key):
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + key)

    def test_staff_can_read_but_not_create(self):
        self.auth(self.staff)
        self.assertEqual(self.client.get('/api/accounts/').status_code, 200)
        res = self.client.post('/api/accounts/', {'code': 'T1', 'name': 'Test', 'account_type': 'Asset'}, format='json')
        self.assertEqual(res.status_code, 403)

    def test_manager_can_create_but_not_delete(self):
        self.auth(self.manager)
        res = self.client.post('/api/accounts/', {'code': 'T2', 'name': 'Test2', 'account_type': 'Asset'}, format='json')
        self.assertEqual(res.status_code, 201)
        acc_id = res.data['id']
        self.assertEqual(self.client.delete(f'/api/accounts/{acc_id}/').status_code, 403)

    def test_admin_can_delete(self):
        self.auth(self.admin)
        res = self.client.post('/api/accounts/', {'code': 'T3', 'name': 'Test3', 'account_type': 'Asset'}, format='json')
        acc_id = res.data['id']
        self.assertEqual(self.client.delete(f'/api/accounts/{acc_id}/').status_code, 204)

    def test_unauthenticated_blocked(self):
        self.assertEqual(self.client.get('/api/accounts/').status_code, 401)


@override_settings(REST_FRAMEWORK=NO_THROTTLE)
class AuditTests(APITestCase):
    def setUp(self):
        _, self.admin = make_user('admin2', Profile.ROLE_ADMIN)
        _, self.staff = make_user('staff2', Profile.ROLE_STAFF)

    def test_write_is_audited(self):
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.admin)
        self.client.post('/api/partners/', {'name': 'Audited Co', 'partner_type': 'Customer'}, format='json')
        self.assertTrue(ActivityLog.objects.filter(action='create', model_name='Partner').exists())

    def test_activity_log_is_admin_only(self):
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.staff)
        self.assertEqual(self.client.get('/api/activity-logs/').status_code, 403)
        self.client.credentials(HTTP_AUTHORIZATION='Token ' + self.admin)
        self.assertEqual(self.client.get('/api/activity-logs/').status_code, 200)
