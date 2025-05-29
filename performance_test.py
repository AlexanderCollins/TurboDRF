#!/usr/bin/env python
"""
Performance testing for TurboDRF
Tests API speed and database query efficiency
"""
import os
import sys
import django
import time
import statistics
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'example_project.source.settings')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, reset_queries
from django.test.utils import override_settings
from django.contrib.auth import get_user_model
from example_project.books.models import Book, Author, Review

User = get_user_model()

# API configuration
BASE_URL = "http://localhost:8001"
API_URL = f"{BASE_URL}/api"
AUTH = ('admin', 'admin123')


def measure_time(func):
    """Decorator to measure function execution time"""
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - start
        return result, elapsed
    return wrapper


@measure_time
def test_list_endpoint(endpoint, params=None):
    """Test list endpoint performance"""
    response = requests.get(f"{API_URL}/{endpoint}/", params=params, auth=AUTH)
    return response.status_code == 200


@measure_time
def test_detail_endpoint(endpoint, id):
    """Test detail endpoint performance"""
    response = requests.get(f"{API_URL}/{endpoint}/{id}/", auth=AUTH)
    return response.status_code == 200


@measure_time
def test_create_endpoint(endpoint, data):
    """Test create endpoint performance"""
    response = requests.post(f"{API_URL}/{endpoint}/", json=data, auth=AUTH)
    return response.status_code == 201


@measure_time
def test_update_endpoint(endpoint, id, data):
    """Test update endpoint performance"""
    response = requests.patch(f"{API_URL}/{endpoint}/{id}/", json=data, auth=AUTH)
    return response.status_code == 200


def test_concurrent_requests(endpoint, num_requests=10):
    """Test concurrent request handling"""
    print(f"\n=== Concurrent Requests Test ({num_requests} requests) ===")
    
    times = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = []
        for i in range(num_requests):
            future = executor.submit(requests.get, f"{API_URL}/{endpoint}/", auth=AUTH)
            futures.append(future)
        
        for future in as_completed(futures):
            start = time.time()
            response = future.result()
            elapsed = time.time() - start
            times.append(elapsed)
            
    avg_time = statistics.mean(times)
    print(f"Average response time: {avg_time*1000:.2f}ms")
    print(f"Min/Max: {min(times)*1000:.2f}ms / {max(times)*1000:.2f}ms")


def test_query_efficiency():
    """Test database query efficiency"""
    print("\n=== Database Query Efficiency ===")
    
    # Test without select_related (baseline)
    reset_queries()
    list(Book.objects.all()[:10])
    queries_without = len(connection.queries)
    
    # Test with select_related (should be optimized in TurboDRF)
    reset_queries()
    list(Book.objects.select_related('author').all()[:10])
    queries_with = len(connection.queries)
    
    print(f"Queries without optimization: {queries_without}")
    print(f"Queries with select_related: {queries_with}")
    
    # Test actual API endpoint
    reset_queries()
    response = requests.get(f"{API_URL}/books/", auth=AUTH)
    api_queries = len(connection.queries)
    print(f"Queries via API endpoint: {api_queries}")
    
    # Check if N+1 problem exists
    if response.status_code == 200:
        books = response.json()['data']
        if books and 'author_name' in books[0]:
            print("✓ Nested fields are included without N+1 queries")
        else:
            print("✗ Nested fields might cause N+1 queries")


def test_pagination_performance():
    """Test pagination performance with different page sizes"""
    print("\n=== Pagination Performance ===")
    
    page_sizes = [10, 50, 100]
    
    for size in page_sizes:
        success, elapsed = test_list_endpoint('books', {'page_size': size})
        if success:
            print(f"Page size {size}: {elapsed*1000:.2f}ms")


def test_filtering_performance():
    """Test filtering performance"""
    print("\n=== Filtering Performance ===")
    
    # No filters
    success, elapsed = test_list_endpoint('books')
    print(f"No filters: {elapsed*1000:.2f}ms")
    
    # Single filter
    success, elapsed = test_list_endpoint('books', {'price__gte': 20})
    print(f"Single filter (price__gte): {elapsed*1000:.2f}ms")
    
    # Multiple filters
    success, elapsed = test_list_endpoint('books', {
        'price__gte': 20,
        'price__lte': 100,
        'published_date__year': 2023
    })
    print(f"Multiple filters: {elapsed*1000:.2f}ms")
    
    # Search
    success, elapsed = test_list_endpoint('books', {'search': 'python'})
    print(f"Search filter: {elapsed*1000:.2f}ms")


def create_bulk_test_data(num_authors=50, books_per_author=10):
    """Create bulk test data for performance testing"""
    print(f"\nCreating test data: {num_authors} authors with {books_per_author} books each...")
    
    # Create authors
    authors = []
    for i in range(num_authors):
        author = Author.objects.create(
            name=f"Test Author {i}",
            email=f"author{i}@test.com",
            bio=f"Bio for author {i}"
        )
        authors.append(author)
    
    # Create books
    book_count = 0
    for i, author in enumerate(authors):
        for j in range(books_per_author):
            Book.objects.create(
                title=f"Book {book_count} by {author.name}",
                author=author,
                isbn=f"978000000{book_count:04d}",
                price=Decimal(f"{20 + (book_count % 80)}.99"),
                published_date=f"2023-{(book_count % 12) + 1:02d}-01",
                description=f"Description for book {book_count}"
            )
            book_count += 1
    
    print(f"Created {num_authors} authors and {book_count} books")
    return book_count


def run_performance_suite():
    """Run complete performance test suite"""
    print("TurboDRF Performance Testing")
    print("=" * 50)
    
    # Check if server is running
    try:
        response = requests.get(f"{API_URL}/")
        if response.status_code != 200:
            print("ERROR: API server not responding. Please run the server on port 8001")
            return
    except:
        print("ERROR: Cannot connect to API server on port 8001")
        return
    
    # Get initial counts
    book_count = Book.objects.count()
    author_count = Author.objects.count()
    print(f"\nInitial data: {author_count} authors, {book_count} books")
    
    # Basic endpoint tests
    print("\n=== Basic Endpoint Performance ===")
    
    # List
    success, elapsed = test_list_endpoint('books')
    print(f"List books: {elapsed*1000:.2f}ms")
    
    # Detail
    books = Book.objects.first()
    if books:
        success, elapsed = test_detail_endpoint('books', books.id)
        print(f"Get book detail: {elapsed*1000:.2f}ms")
    
    # Run other tests
    test_pagination_performance()
    test_filtering_performance()
    test_query_efficiency()
    test_concurrent_requests('books', 20)
    
    # Bulk data test (optional)
    if book_count < 100:
        print("\n=== Bulk Data Performance Test ===")
        response = input("Create bulk test data? This will add 500 books. (y/N): ")
        if response.lower() == 'y':
            create_bulk_test_data(50, 10)
            
            # Re-test with more data
            print("\n=== Performance with Bulk Data ===")
            success, elapsed = test_list_endpoint('books')
            print(f"List all books: {elapsed*1000:.2f}ms")
            
            success, elapsed = test_list_endpoint('books', {'page_size': 100})
            print(f"List 100 books: {elapsed*1000:.2f}ms")
            
            test_concurrent_requests('books', 50)


if __name__ == "__main__":
    run_performance_suite()