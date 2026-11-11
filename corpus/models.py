import uuid
from django.db import models
from django.utils import timezone


class Study(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        "auth.User", on_delete=models.PROTECT, related_name="owned_studies"
    )
    created = models.DateTimeField(auto_now_add=True)
    notification_emails = models.JSONField(default=list)
    codebook = models.ForeignKey(
        "SharedCodebook",
        on_delete=models.PROTECT,
        null=True,
        related_name="current_for",
    )
    instructions = models.ForeignKey(
        "StudyProtocol", on_delete=models.PROTECT, null=True, related_name="current_for"
    )

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        for field in ("codebook", "instructions"):
            value = getattr(self, field)
            if value and value.study_id != self.pk:
                raise ValueError("Study revisions must belong to this study")
        return super().save(*args, **kwargs)


class StudyAdmin(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="admins")
    user = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    granted_by = models.ForeignKey(
        "auth.User", on_delete=models.PROTECT, related_name="study_grants"
    )
    created = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["study", "user"], name="study_admin_unique")
        ]


class StudyAdminInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="admin_invitations"
    )
    username = models.CharField(max_length=140)
    email = models.EmailField(blank=True)
    invited_by = models.ForeignKey(
        "auth.User", on_delete=models.PROTECT, related_name="study_invitations"
    )
    created = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "username"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="pending_study_invitation_unique",
            )
        ]


class ResearchQuestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="research_questions"
    )
    label = models.CharField(max_length=40)
    text = models.TextField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "label", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["study", "label"], name="study_question_label_unique"
            )
        ]


class CodingGuideline(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="guidelines"
    )
    title = models.CharField(max_length=200)
    text = models.TextField()
    position = models.PositiveIntegerField(default=0)
    questions = models.ManyToManyField(ResearchQuestion, blank=True)

    class Meta:
        ordering = ["position", "title", "pk"]


class StudyProtocol(models.Model):
    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="protocol_revisions"
    )
    content = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Published instructions are immutable")
        return super().save(*args, **kwargs)


class Case(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="cases")
    key = models.CharField(max_length=255)
    name = models.CharField(max_length=500)
    metadata = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "key"], name="study_case_key_unique"
            )
        ]

    def __str__(self):
        return self.name


class Document(models.Model):
    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="documents")
    key = models.CharField(max_length=500)
    name = models.CharField(max_length=500)
    cases = models.ManyToManyField(Case)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "key"], name="study_document_key_unique"
            )
        ]

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
    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="codebook_revisions"
    )
    content = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk and SharedCodebook.objects.filter(pk=self.pk).exists():
            raise ValueError("Shared revisions are immutable")
        return super().save(*args, **kwargs)


class CodingSession(models.Model):
    admin_user = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="admin_coding_sessions",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["study", "admin_user"],
                condition=models.Q(deleted_at__isnull=True, admin_user__isnull=False),
                name="active_admin_study_session_unique",
            )
        ]

    study = models.ForeignKey(
        Study, on_delete=models.CASCADE, related_name="coder_sessions"
    )
    instructions = models.ForeignKey(StudyProtocol, on_delete=models.PROTECT)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coder = models.CharField(max_length=200)
    email = models.EmailField(blank=True, default="")
    deleted_at = models.DateTimeField(null=True, blank=True)
    base = models.ForeignKey(SharedCodebook, on_delete=models.PROTECT)
    codebook = models.JSONField()
    revision = models.PositiveIntegerField(default=0)
    token_hash = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return self.coder

    def save(self, *args, **kwargs):
        if (
            self.base.study_id != self.study_id
            or self.instructions.study_id != self.study_id
        ):
            raise ValueError(
                "Coder codebook and instructions must belong to the session study"
            )
        return super().save(*args, **kwargs)


class Assignment(models.Model):
    removed_at = models.DateTimeField(null=True, blank=True)
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

    def save(self, *args, **kwargs):
        if self.session.study_id != self.case.study_id:
            raise ValueError("Case and coder must belong to the same study")
        return super().save(*args, **kwargs)


class Annotation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT)
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    selected_text = models.TextField()
    codes = models.JSONField(default=list)
    memo = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.assignment.documents.filter(pk=self.document_id).exists():
            raise ValueError("Annotation document must be assigned to this case")
        return super().save(*args, **kwargs)


class Submission(models.Model):
    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT)
    snapshot = models.JSONField()
    created = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Submissions are immutable")
        return super().save(*args, **kwargs)


class SubmissionWithdrawal(models.Model):
    submission = models.OneToOneField(
        Submission, on_delete=models.PROTECT, related_name="withdrawal"
    )
    initiated_by = models.CharField(
        max_length=10, choices=[("coder", "Coder"), ("admin", "Admin")]
    )
    created = models.DateTimeField(auto_now_add=True)


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
    study = models.ForeignKey(Study, on_delete=models.CASCADE, related_name="reports")
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    created = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey("auth.User", on_delete=models.PROTECT)
    payload = models.JSONField()

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValueError("Saved reports are immutable")
        return super().save(*args, **kwargs)


class StudyAdminEmail(EmailDelivery):
    invitation = models.OneToOneField(
        StudyAdminInvitation, on_delete=models.CASCADE, related_name="email_delivery"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)

    @property
    def status(self):
        return "Cancelled" if self.cancelled_at else super().status
