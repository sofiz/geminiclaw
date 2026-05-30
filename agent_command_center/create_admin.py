import os
import django

# Initialize Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'agent_command_center.settings')
django.setup()

from django.contrib.auth.models import User

def init_admin():
    username = 'admin'
    password = 'antigravity-secure-2026'
    email = 'admin@localhost'

    try:
        user, created = User.objects.get_or_create(username=username, defaults={
            'email': email,
            'is_superuser': True,
            'is_staff': True
        })
        user.set_password(password)
        # Ensure staff and superuser permissions are enabled
        user.is_superuser = True
        user.is_staff = True
        user.save()
        if created:
            print(f"SUCCESS: Superuser '{username}' successfully created with password '{password}'.")
        else:
            print(f"SUCCESS: Superuser '{username}' password successfully updated to '{password}'.")
    except Exception as e:
        print(f"ERROR: Failed to initialize administrative credentials: {e}")

if __name__ == '__main__':
    init_admin()
