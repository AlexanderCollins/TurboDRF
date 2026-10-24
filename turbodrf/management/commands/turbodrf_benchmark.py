"""
Benchmark compiled vs DRF read paths for a model.

Each timed call is a real list request, as the given user, through the
model's registered viewset (permissions, tenant and predicate scoping,
filters, pagination and rendering included), once on the compiled path and
once on the DRF serializer path.

Usage:
    python manage.py turbodrf_benchmark ModelName --user alice
    python manage.py turbodrf_benchmark ModelName --user alice --requests 500
    python manage.py turbodrf_benchmark ModelName --user alice --page-size 50
"""

import time

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from turbodrf.mixins import TurboDRFMixin


def _registered_viewset(model):
    """(viewset class, initkwargs) of ``model``'s list route in the URLconf,
    or None."""
    from django.urls import URLResolver, get_resolver

    from turbodrf.views import TurboDRFViewSet

    patterns = list(get_resolver().url_patterns)
    while patterns:
        pattern = patterns.pop()
        if isinstance(pattern, URLResolver):
            patterns.extend(pattern.url_patterns)
            continue
        callback = getattr(pattern, "callback", None)
        cls = getattr(callback, "cls", None)
        if (
            isinstance(cls, type)
            and issubclass(cls, TurboDRFViewSet)
            and getattr(cls, "model", None) is model
            and (getattr(callback, "actions", None) or {}).get("get") == "list"
        ):
            return cls, dict(getattr(callback, "initkwargs", None) or {})
    return None


class Command(BaseCommand):
    help = "Benchmark compiled vs DRF read paths for a TurboDRF model"

    def add_arguments(self, parser):
        parser.add_argument("model_name", type=str, help="Model name to benchmark")
        parser.add_argument(
            "--user",
            type=str,
            required=True,
            help="Username to send the requests as (permissions and row "
            "scoping apply as for that user)",
        )
        parser.add_argument(
            "--requests",
            type=int,
            default=100,
            help="Number of requests to simulate (default: 100)",
        )
        parser.add_argument(
            "--page-size",
            type=int,
            default=20,
            help="Page size for list requests (default: 20)",
        )
        parser.add_argument(
            "--warmup",
            type=int,
            default=10,
            help="Number of warmup requests (default: 10)",
        )

    def handle(self, *args, **options):
        from turbodrf.compiler import is_compiled

        model_name = options["model_name"]
        username = options["user"]
        num_requests = options["requests"]
        page_size = options["page_size"]
        warmup = options["warmup"]

        # Find the model
        model = None
        for m in apps.get_models():
            if issubclass(m, TurboDRFMixin) and m.__name__ == model_name:
                model = m
                break

        if model is None:
            raise CommandError(f"No TurboDRF model named '{model_name}' found.")
        if num_requests < 1:
            raise CommandError("--requests must be at least 1.")

        User = get_user_model()
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError(f"No user '{username}'.")

        registered = _registered_viewset(model)
        if registered is None:
            raise CommandError(f"{model_name} has no list route in the URLconf.")
        viewset, initkwargs = registered

        # The DRF serializer path of the same viewset.
        serializer_viewset = type(
            f"{viewset.__name__}SerializerPath",
            (viewset,),
            {"_should_use_compiled_path": lambda self, request: False},
        )
        drf_view = serializer_viewset.as_view({"get": "list"}, **initkwargs)

        first = self._request(drf_view, user, page_size)
        if first.status_code != 200:
            raise CommandError(
                f"GET {model_name} list as {username} returned "
                f"{first.status_code}: {getattr(first, 'data', '')}"
            )
        data = first.data
        count = (
            data["pagination"]["total_items"]
            if isinstance(data, dict) and "pagination" in data
            else len(data)
        )
        if not count:
            raise CommandError(
                f"{model_name} has no data visible to {username}. "
                "Create some objects first."
            )

        self.stdout.write(
            f"\nBenchmarking {model_name} as {username} "
            f"({count} objects, page_size={page_size})"
        )
        self.stdout.write(f"Requests: {num_requests} (+ {warmup} warmup)\n")

        drf_times = self._benchmark(drf_view, user, num_requests, warmup, page_size)
        drf_avg, drf_p95 = _summary(drf_times)

        self.stdout.write(f"{'Path':<12} {'Avg':>10} {'p95':>10}")
        self.stdout.write(f"{'-'*34}")
        self.stdout.write(f"{'DRF':<12} {drf_avg:>8.2f}ms {drf_p95:>8.2f}ms")

        if not is_compiled(model):
            self.stdout.write(
                f"\n{model_name} isn't served by the compiled path "
                "(see turbodrf_check); nothing to compare."
            )
            return

        compiled_view = viewset.as_view({"get": "list"}, **initkwargs)
        compiled_times = self._benchmark(
            compiled_view, user, num_requests, warmup, page_size
        )
        compiled_avg, compiled_p95 = _summary(compiled_times)
        speedup = drf_avg / compiled_avg if compiled_avg > 0 else 0

        self.stdout.write(
            f"{'Compiled':<12} {compiled_avg:>8.2f}ms {compiled_p95:>8.2f}ms"
        )
        self.stdout.write(f"\nSpeedup: {self.style.SUCCESS(f'{speedup:.1f}x')}")

    def _request(self, view, user, page_size):
        response = view(_list_request(user, page_size))
        response.render()
        return response

    def _benchmark(self, view, user, num_requests, warmup, page_size):
        """Seconds per list request (render included), after warmup."""
        for _ in range(warmup):
            self._request(view, user, page_size)

        times = []
        for _ in range(num_requests):
            request = _list_request(user, page_size)
            start = time.perf_counter()
            view(request).render()
            times.append(time.perf_counter() - start)
        return times


def _list_request(user, page_size):
    from rest_framework.test import APIRequestFactory, force_authenticate

    request = APIRequestFactory().get("/", {"page_size": page_size})
    force_authenticate(request, user=user)
    return request


def _summary(times):
    """(average, p95) in milliseconds."""
    ordered = sorted(times)
    average = sum(ordered) / len(ordered) * 1000
    p95 = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)] * 1000
    return average, p95
