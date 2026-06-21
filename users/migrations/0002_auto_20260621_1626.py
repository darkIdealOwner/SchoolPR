from django.db import migrations
from django.contrib.auth.hashers import make_password

def seed_users(apps, schema_editor):
    User = apps.get_model('users', 'User')
    users_data = [
        {
            'username': 'owner',
            'first_name': 'Владелец',
            'phone_number': '+70000000001',
            'role': 'OWNER',
            'password': make_password('qwerty123'),
        },
        {
            'username': 'director',
            'first_name': 'Директор',
            'phone_number': '+70000000002',
            'role': 'DIRECTOR',
            'password': make_password('qwerty123'),
        },
        {
            'username': 'employee',
            'first_name': 'Сотрудник',
            'phone_number': '+70000000003',
            'role': 'EMPLOYEE',
            'password': make_password('qwerty123'),
        },
        {
            'username': 'investor',
            'first_name': 'Инвестор',
            'phone_number': '+70000000004',
            'role': 'INVESTOR',
            'password': make_password('qwerty123'),
        },
    ]
    
    for data in users_data:
        User.objects.create(**data)

class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_users),
    ]
