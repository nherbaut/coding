from copy import deepcopy
from html.parser import HTMLParser
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Assignment, Case, SharedCodebook
from .services import create_session, submit


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class ReadableReviewTests(TestCase):
    def test_names_memos_moves_and_references_are_readable_without_identifiers(self):
        parent, other, leaf, addition, removed = [str(uuid.uuid4()) for _ in range(5)]
        book = dict(version=1, codes=[
            dict(id=parent, kind='category', name='Social', parent=None),
            dict(id=other, kind='category', name='Individual', parent=None),
            dict(id=leaf, kind='code', name='Review burden', parent=parent, memo='Original evidence'),
            dict(id=removed, kind='code', name='Retired concept', parent=None),
        ])
        SharedCodebook.objects.create(content=book)
        case = Case.objects.create(key='project', name='Project')
        session, _ = create_session('Alice', [case])
        incoming = deepcopy(book)
        incoming['codes'][2].update(name='Maintainer capacity', parent=other, memo='New evidence')
        incoming['codes'].pop()
        incoming['codes'].append(dict(id=addition, kind='code', name='New concept', parent=parent,
            references=[dict(title='Corpus policy', quote='Quoted passage', location='Section 2')]))
        session.codebook = incoming
        session.save()
        submission = submit(Assignment.objects.get(session=session).pk, session.pk, 0)
        # Include a concurrent shared edit in the comparison.
        current = deepcopy(book)
        current['codes'][2]['memo'] = 'Shared evidence'
        SharedCodebook.objects.create(content=current)
        admin = get_user_model().objects.create_superuser('admin', 'admin@example.org', 'password')
        self.client.force_login(admin)
        response = self.client.get(f'/review/{submission.pk}/')
        self.assertEqual(response.status_code, 200)
        parser = VisibleText()
        parser.feed(response.content.decode())
        visible = ' '.join(parser.parts)
        for expected in ['Updated code', 'Maintainer capacity', 'Review burden', 'Original evidence',
                         'New evidence', 'Shared evidence', 'Individual', 'Social', 'New code',
                         'Removed code', 'Corpus policy', 'Quoted passage', 'Section 2']:
            self.assertIn(expected, visible)
        for identity in [parent, other, leaf, addition, removed]:
            self.assertNotIn(identity, visible)
        self.assertNotIn("'id':", visible)
        self.assertNotIn('supercid', visible)
        self.assertContains(response, 'change-details')
