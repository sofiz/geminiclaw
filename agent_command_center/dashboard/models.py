from django.db import models
import uuid

class AgentInstance(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    workspace = models.CharField(max_length=255)
    model_name = models.CharField(max_length=100, default='gemini-3.5-flash')
    pid = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, default='stopped') # stopped, running, paused, terminated, error
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.id}) - {self.status}"

