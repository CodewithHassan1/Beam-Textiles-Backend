from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = 'Delete all users and create a fresh superuser for production.'

    def handle(self, *args, **options):
        User = get_user_model()

        # Delete ALL existing users (removes hardcoded demo accounts)
        count = User.objects.count()
        User.objects.all().delete()
        self.stdout.write(self.style.WARNING(f'Deleted {count} existing user(s).'))

        # Create the production superuser
        User.objects.create_superuser(
            username='admin',
            email='hassanhaiderwk@gmail.com',
            password='password',
        )
        self.stdout.write(self.style.SUCCESS(
            'Superuser created: username=admin | email=hassanhaiderwk@gmail.com'
        ))
