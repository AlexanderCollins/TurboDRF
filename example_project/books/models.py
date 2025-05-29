from django.db import models

from turbodrf.mixins import TurboDRFMixin


class Author(TurboDRFMixin, models.Model):
    name = models.CharField(max_length=100)
    email = models.EmailField()
    bio = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {"fields": ["id", "name", "email", "bio", "created_at"]}


class Book(TurboDRFMixin, models.Model):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE, related_name="books")
    isbn = models.CharField(max_length=13, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    published_date = models.DateField()
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    searchable_fields = ["title", "isbn", "description"]

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "fields": {
                "list": [
                    "id",
                    "title",
                    "author",
                    "author__name",
                    "price",
                    "published_date",
                ],
                "detail": [
                    "id",
                    "title",
                    "author",
                    "author__name",
                    "author__email",
                    "isbn",
                    "price",
                    "published_date",
                    "description",
                    "created_at",
                ],
            }
        }


class Review(TurboDRFMixin, models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE, related_name="reviews")
    reviewer_name = models.CharField(max_length=100)
    rating = models.IntegerField(choices=[(i, i) for i in range(1, 6)])
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.book.title} - {self.rating} stars"

    @classmethod
    def turbodrf(cls):
        return {
            "fields": [
                "id",
                "book",
                "book__title",
                "reviewer_name",
                "rating",
                "comment",
                "created_at",
            ]
        }
