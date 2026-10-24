"""Create the example stores, users and books. Safe to run more than once."""

from django.core.management.base import BaseCommand

from books.models import Author, Book, Store, User

USERS = [
    # username, store, role
    ("admin", "Riverside Books", "admin"),
    ("editor", "Riverside Books", "editor"),
    ("viewer", "Riverside Books", "viewer"),
    ("other_editor", "Hillside Books", "editor"),
]

BOOKS = [
    # store, title, author, price
    ("Riverside Books", "The Dispossessed", "Ursula K. Le Guin", "12.50"),
    ("Riverside Books", "A Wizard of Earthsea", "Ursula K. Le Guin", "9.99"),
    ("Hillside Books", "Kindred", "Octavia E. Butler", "14.00"),
]


class Command(BaseCommand):
    help = "Create the example stores, users and books."

    def handle(self, *args, **options):
        for username, store_name, role in USERS:
            store, _ = Store.objects.get_or_create(name=store_name)
            user, _ = User.objects.get_or_create(username=username)
            user.store = store
            user.role = role
            user.set_password(f"{username}-pass")
            user.save()

        for store_name, title, author_name, price in BOOKS:
            Book.objects.get_or_create(
                store=Store.objects.get(name=store_name),
                title=title,
                defaults={
                    "author": Author.objects.get_or_create(name=author_name)[0],
                    "price": price,
                },
            )

        self.stdout.write("Users (password: <username>-pass):")
        for username, store_name, role in USERS:
            self.stdout.write(f"  {username:<13} {role:<7} {store_name}")
