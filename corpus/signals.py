from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from .models import Document, Assignment, CodingGuideline


@receiver(m2m_changed, sender=Document.cases.through)
def document_cases(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add":
        return
    if reverse:
        if (
            Document.objects.filter(pk__in=pk_set)
            .exclude(study_id=instance.study_id)
            .exists()
        ):
            raise ValueError("Document and cases must belong to the same study")
    else:
        if (
            instance.cases.model.objects.filter(pk__in=pk_set)
            .exclude(study_id=instance.study_id)
            .exists()
        ):
            raise ValueError("Document and cases must belong to the same study")


@receiver(m2m_changed, sender=Assignment.documents.through)
def assignment_documents(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add":
        return
    if reverse:
        for assignment in Assignment.objects.filter(pk__in=pk_set).select_related(
            "case", "session"
        ):
            if (
                instance.document.study_id != assignment.session.study_id
                or not instance.document.cases.filter(pk=assignment.case_id).exists()
            ):
                raise ValueError("Assigned document must belong to this study and case")
    else:
        for version in instance.documents.model.objects.filter(
            pk__in=pk_set
        ).select_related("document"):
            if (
                version.document.study_id != instance.session.study_id
                or not version.document.cases.filter(pk=instance.case_id).exists()
            ):
                raise ValueError("Assigned document must belong to this study and case")


@receiver(m2m_changed, sender=CodingGuideline.questions.through)
def guideline_questions(sender, instance, action, reverse, pk_set, **kwargs):
    if action != "pre_add":
        return
    model = CodingGuideline if reverse else instance.questions.model
    if model.objects.filter(pk__in=pk_set).exclude(study_id=instance.study_id).exists():
        raise ValueError(
            "Guideline and research questions must belong to the same study"
        )
