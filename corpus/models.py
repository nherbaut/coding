import uuid
from django.db import models
from django.utils import timezone


class Case(models.Model):
    key = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict)

    def __str__(self):
        return self.name


class Document(models.Model):
    key = models.CharField(max_length=500, unique=True)
    name = models.CharField(max_length=500)
    cases = models.ManyToManyField(Case)

    def __str__(self):
        return self.name


class DocumentVersion(models.Model):
    document = models.ForeignKey(Document, on_delete=models.PROTECT)
    text = models.TextField()
    sha256 = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sha256"], name="document_content_unique"
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Document versions are immutable")
        return super().save(*args, **kwargs)


class SharedCodebook(models.Model):
    content = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk and SharedCodebook.objects.filter(pk=self.pk).exists():
            raise ValueError("Shared revisions are immutable")
        return super().save(*args, **kwargs)


class CodingSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coder = models.CharField(max_length=200)
    email = models.EmailField(blank=True, default="")
    base = models.ForeignKey(SharedCodebook, on_delete=models.PROTECT)
    codebook = models.JSONField()
    revision = models.PositiveIntegerField(default=0)
    token_hash = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return self.coder


class Assignment(models.Model):
    session = models.ForeignKey(CodingSession, on_delete=models.PROTECT)
    case = models.ForeignKey(Case, on_delete=models.PROTECT)
    documents = models.ManyToManyField(DocumentVersion)
    locked = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "case"], name="session_case_unique"
            )
        ]


class Annotation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT)
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    selected_text = models.TextField()
    codes = models.JSONField(default=list)
    memo = models.TextField(blank=True)


class Submission(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    snapshot = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Submissions are immutable")
        return super().save(*args, **kwargs)


class MergeAudit(models.Model):
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT)
    admin = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    previous = models.ForeignKey(
        SharedCodebook, on_delete=models.PROTECT, related_name="+"
    )
    result = models.ForeignKey(
        SharedCodebook, on_delete=models.PROTECT, related_name="+"
    )
    decisions = models.JSONField()
    mappings = models.JSONField(default=dict)
    created = models.DateTimeField(auto_now_add=True)


class EmailDelivery(models.Model):
    subject = models.CharField(max_length=250)
    body = models.TextField()
    sender = models.CharField(max_length=254)
    recipients = models.JSONField()
    message_id = models.CharField(max_length=254)
    created = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=100, blank=True)

    class Meta:
        abstract = True

    @property
    def status(self):
        if self.sent_at:
            return "Sent"
        return "Retrying" if self.attempts else "Queued"


class SubmissionEmail(EmailDelivery):
    submission = models.OneToOneField(
        Submission, on_delete=models.PROTECT, related_name="email_notification"
    )


class InvitationEmail(EmailDelivery):
    session = models.ForeignKey(
        CodingSession, on_delete=models.PROTECT, related_name="invitation_emails"
    )
    token_hash = models.CharField(max_length=64)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    @property
    def status(self):
        if self.cancelled_at:
            return "Cancelled"
        if self.token_hash != self.session.token_hash:
            return "Replaced"
        return super().status


class CodingReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    payload = models.JSONField()

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Saved reports are immutable")
        return super().save(*args, **kwargs)
