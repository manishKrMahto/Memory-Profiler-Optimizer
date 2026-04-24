from django.db import models


class Repository(models.Model):
    name = models.CharField(max_length=255)
    path = models.TextField(help_text="Absolute path on server disk where repo is stored.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}:{self.name}"


class RepoFile(models.Model):
    repo = models.ForeignKey(Repository, on_delete=models.CASCADE, related_name="files")
    file_path = models.TextField(help_text="Repo-relative path using forward slashes.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("repo", "file_path")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.repo_id}:{self.file_path}"


class Function(models.Model):
    file = models.ForeignKey(RepoFile, on_delete=models.CASCADE, related_name="functions")
    function_name = models.CharField(max_length=512, help_text="Qualified name (e.g. Class.method or func).")
    start_line = models.IntegerField()
    end_line = models.IntegerField()
    original_code = models.TextField()
    optimized_code = models.TextField(blank=True, default="")
    decision = models.CharField(
        max_length=16,
        choices=[("pending", "pending"), ("accepted", "accepted"), ("rejected", "rejected")],
        default="pending",
    )
    preferred_version = models.CharField(
        max_length=16,
        choices=[("original", "original"), ("optimized", "optimized")],
        default="original",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("file", "function_name", "start_line", "end_line")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.id}:{self.function_name}"


class ProfilingResult(models.Model):
    VERSION_CHOICES = [
        ("original", "original"),
        ("optimized", "optimized"),
    ]

    function = models.ForeignKey(Function, on_delete=models.CASCADE, related_name="profiling_results")
    version = models.CharField(max_length=16, choices=VERSION_CHOICES)
    memory_usage = models.JSONField(default=list, blank=True)
    peak_memory = models.FloatField(null=True, blank=True)
    execution_time = models.FloatField(null=True, blank=True, help_text="Seconds")
    profiler_output = models.TextField(blank=True, default="", help_text="Raw memory_profiler text output (best-effort).")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["function", "version"])]
