"""
Benchmark compiled vs DRF read paths for a model.

Usage:
    python manage.py turbodrf_benchmark ModelName
    python manage.py turbodrf_benchmark ModelName --requests 500
    python manage.py turbodrf_benchmark ModelName --page-size 50
"""

import time

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from turbodrf.compiler import compile_model
from turbodrf.mixins import TurboDRFMixin


class Command(BaseCommand):
    help = "Benchmark compiled vs DRF read paths for a TurboDRF model"

    def add_arguments(self, parser):
        parser.add_argument("model_name", type=str, help="Model name to benchmark")
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
        model_name = options["model_name"]
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

        count = model.objects.count()
        if count == 0:
            raise CommandError(f"{model_name} has no data. Create some objects first.")

        self.stdout.write(
            f"\nBenchmarking {model_name} ({count} objects, page_size={page_size})"
        )
        self.stdout.write(f"Requests: {num_requests} (+ {warmup} warmup)\n")

        # Benchmark DRF path
        drf_times = self._benchmark_drf(model, num_requests, warmup, page_size)

        # Benchmark compiled path
        plan = compile_model(model)
        if plan is None:
            # Temporarily compile it for the benchmark
            config = model.turbodrf()
            config["compiled"] = True
            # Monkey-patch temporarily
            original_turbodrf = model.turbodrf
            model.turbodrf = classmethod(lambda cls: config).__get__(model, type(model))
            plan = compile_model(model)
            model.turbodrf = original_turbodrf

        if plan is None:
            raise CommandError(f"Could not compile {model_name}.")

        compiled_times = self._benchmark_compiled(
            model, plan, num_requests, warmup, page_size
        )

        # Results
        drf_avg = sum(drf_times) / len(drf_times) * 1000
        drf_p95 = sorted(drf_times)[int(len(drf_times) * 0.95)] * 1000
        compiled_avg = sum(compiled_times) / len(compiled_times) * 1000
        compiled_p95 = sorted(compiled_times)[int(len(compiled_times) * 0.95)] * 1000
        speedup = drf_avg / compiled_avg if compiled_avg > 0 else 0

        self.stdout.write(f"{'Path':<12} {'Avg':>10} {'p95':>10}")
        self.stdout.write(f"{'-'*34}")
        self.stdout.write(f"{'DRF':<12} {drf_avg:>8.2f}ms {drf_p95:>8.2f}ms")
        self.stdout.write(
            f"{'Compiled':<12} {compiled_avg:>8.2f}ms {compiled_p95:>8.2f}ms"
        )
        self.stdout.write(f"\nSpeedup: {self.style.SUCCESS(f'{speedup:.1f}x')}")

    def _benchmark_drf(self, model, num_requests, warmup, page_size):
        """Benchmark the DRF serializer path."""
        from turbodrf.serializers import TurboDRFSerializer

        config = model.turbodrf()
        fields_config = config.get("fields", "__all__")
        if isinstance(fields_config, dict):
            list_fields = fields_config.get("list", "__all__")
        else:
            list_fields = fields_config

        queryset = model.objects.all()[:page_size]

        # Create serializer class
        meta_attrs = {
            "model": model,
            "fields": "__all__" if list_fields == "__all__" else list_fields,
        }
        SerializerClass = type(
            f"{model.__name__}BenchSerializer",
            (TurboDRFSerializer,),
            {"Meta": type("Meta", (), meta_attrs)},
        )

        # Warmup
        for _ in range(warmup):
            serializer = SerializerClass(queryset, many=True)
            _ = serializer.data

        # Benchmark
        times = []
        for _ in range(num_requests):
            start = time.perf_counter()
            serializer = SerializerClass(queryset, many=True)
            _ = serializer.data
            times.append(time.perf_counter() - start)

        return times

    def _benchmark_compiled(self, model, plan, num_requests, warmup, page_size):
        """Benchmark the compiled read path."""
        queryset = model.objects.all()

        # Warmup
        for _ in range(warmup):
            compiled_qs, active_plan = plan.apply_to_queryset(queryset)
            rows = list(compiled_qs[:page_size])
            plan.post_process(rows, active_plan)

        # Benchmark
        times = []
        for _ in range(num_requests):
            start = time.perf_counter()
            compiled_qs, active_plan = plan.apply_to_queryset(queryset)
            rows = list(compiled_qs[:page_size])
            plan.post_process(rows, active_plan)
            times.append(time.perf_counter() - start)

        return times
