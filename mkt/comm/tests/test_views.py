import json
import os

from django.conf import settings
from django.core import mail
from django.core.urlresolvers import reverse
from django.test.client import MULTIPART_CONTENT
from django.test.utils import override_settings

import mock
from nose.exc import SkipTest
from nose.tools import eq_, ok_

import mkt.constants.comm as comm
from amo.tests import (app_factory, req_factory_factory, user_factory,
                       version_factory)
from mkt.api.tests.test_oauth import RestOAuth
from mkt.comm.models import (CommAttachment, CommunicationNote,
                             CommunicationThread, CommunicationThreadCC)
from mkt.comm.views import EmailCreationPermission, post_email, ThreadPermission
from mkt.site.fixtures import fixture
from mkt.users.models import UserProfile
from mkt.webapps.models import Webapp


TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ATTACHMENTS_DIR = os.path.join(TESTS_DIR, 'attachments')


class CommTestMixin(object):

    def _thread_factory(self, note=False, perms=None, no_perms=None, **kw):
        create_perms = {}
        for perm in perms or []:
            create_perms['read_permission_%s' % perm] = True
        for perm in no_perms or []:
            create_perms['read_permission_%s' % perm] = False
        kw.update(create_perms)

        thread = self.addon.threads.create(**kw)
        if note:
            self._note_factory(thread)
            CommunicationThreadCC.objects.create(user=self.profile,
                                                 thread=thread)
        return thread

    def _note_factory(self, thread, perms=None, no_perms=None, **kw):
        author = kw.pop('author', self.profile)
        body = kw.pop('body', 'something')

        create_perms = {}
        for perm in perms or []:
            create_perms['read_permission_%s' % perm] = True
        for perm in no_perms or []:
            create_perms['read_permission_%s' % perm] = False
        kw.update(create_perms)

        return thread.notes.create(author=author, body=body, **kw)


class AttachmentManagementMixin(object):

    def _attachment_management_form(self, num=1):
        """
        Generate and return data for a management form for `num` attachments
        """
        return {'form-TOTAL_FORMS': max(1, num),
                'form-INITIAL_FORMS': 0,
                'form-MAX_NUM_FORMS': 1000}

    def _attachments(self, num):
        """Generate and return data for `num` attachments """
        data = {}
        files = ['bacon.jpg', 'bacon.txt']
        descriptions = ['mmm, bacon', '']
        for n in xrange(num):
            i = 0 if n % 2 else 1
            path = os.path.join(ATTACHMENTS_DIR, files[i])
            attachment = open(path, 'r')
            data.update({
                'form-%d-attachment' % n: attachment,
                'form-%d-description' % n: descriptions[i]
            })
        return data


class TestThreadDetail(RestOAuth, CommTestMixin):
    fixtures = fixture('webapp_337141', 'user_2519', 'user_support_staff')

    def setUp(self):
        super(TestThreadDetail, self).setUp()
        self.addon = Webapp.objects.get(pk=337141)

    def check_permissions(self, thread):
        req = req_factory_factory(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}),
            user=self.profile)

        return ThreadPermission().has_object_permission(
            req, 'comm-thread-detail', thread)

    def test_response(self):
        thread = self._thread_factory(note=True)

        res = self.client.get(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}))
        eq_(res.status_code, 200)
        eq_(len(res.json['recent_notes']), 1)
        eq_(res.json['addon'], self.addon.id)

    def test_recent_notes_perm(self):
        staff = UserProfile.objects.get(username='support_staff')
        self.addon.addonuser_set.create(user=self.profile)
        thread = self._thread_factory(read_permission_developer=True)
        self._note_factory(
            thread, perms=['developer'], author=staff, body='allowed')
        no_dev_note = self._note_factory(
            thread, no_perms=['developer'], author=staff)

        # Test that the developer can't access no-developer note.
        res = self.client.get(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}))
        eq_(res.status_code, 200)
        eq_(len(res.json['recent_notes']), 1)
        eq_(res.json['recent_notes'][0]['body'], 'allowed')
        eq_(res.json['addon'], self.addon.id)

        # Test that the author always has permissions.
        no_dev_note.update(author=self.profile)
        res = self.client.get(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}))
        eq_(len(res.json['recent_notes']), 2)

    def test_cc(self):
        # Test with no CC.
        thread = self._thread_factory()
        assert not self.check_permissions(thread)

        # Test with CC created.
        thread.thread_cc.create(user=self.profile)
        assert self.check_permissions(thread)

    def test_addon_dev_allowed(self):
        thread = self._thread_factory(perms=['developer'])
        self.addon.addonuser_set.create(user=self.profile)
        assert self.check_permissions(thread)

    def test_addon_dev_denied(self):
        """Test when the user is a developer of a different add-on."""
        thread = self._thread_factory(perms=['developer'])
        self.profile.addonuser_set.create(addon=app_factory())
        assert not self.check_permissions(thread)

    def test_read_public(self):
        thread = self._thread_factory(perms=['public'])
        assert self.check_permissions(thread)

    def test_read_moz_contact(self):
        thread = self._thread_factory(perms=['mozilla_contact'])
        self.addon.update(mozilla_contact=self.profile.email)
        assert self.check_permissions(thread)

    def test_read_reviewer(self):
        thread = self._thread_factory(perms=['reviewer'])
        self.grant_permission(self.profile, 'Apps:Review')
        assert self.check_permissions(thread)

    def test_read_senior_reviewer(self):
        thread = self._thread_factory(perms=['senior_reviewer'])
        self.grant_permission(self.profile, 'Apps:ReviewEscalated')
        assert self.check_permissions(thread)

    def test_read_staff(self):
        thread = self._thread_factory(perms=['staff'])
        self.grant_permission(self.profile, 'Admin:%')
        assert self.check_permissions(thread)

    def test_cors_allowed(self):
        thread = self._thread_factory()

        res = self.client.get(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}))
        self.assertCORS(res, 'get', 'post', 'patch')

    def test_review_url(self):
        thread = self._thread_factory(note=True)

        res = self.client.get(
            reverse('comm-thread-detail', kwargs={'pk': thread.pk}))
        eq_(res.status_code, 200)
        eq_(res.json['addon_meta']['review_url'],
            reverse('reviewers.apps.review', args=[self.addon.app_slug]))

    def test_version_number(self):
        version = version_factory(addon=self.addon, version='7.12')
        thread = CommunicationThread.objects.create(
            addon=self.addon, version=version, read_permission_public=True)

        res = self.client.get(reverse('comm-thread-detail', args=[thread.pk]))
        eq_(json.loads(res.content)['version_number'], '7.12')
        eq_(json.loads(res.content)['version_is_obsolete'], False)

        version.delete()
        res = self.client.get(reverse('comm-thread-detail', args=[thread.pk]))
        eq_(json.loads(res.content)['version_number'], '7.12')
        eq_(json.loads(res.content)['version_is_obsolete'], True)

    def test_app_threads(self):
        version1 = version_factory(addon=self.addon, version='7.12')
        thread1 = CommunicationThread.objects.create(
            addon=self.addon, version=version1, read_permission_public=True)

        version2 = version_factory(addon=self.addon, version='1.16')
        thread2 = CommunicationThread.objects.create(
            addon=self.addon, version=version2, read_permission_public=True)

        for thread in (thread1, thread2):
            res = self.client.get(reverse('comm-thread-detail',
                                  args=[thread.pk]))
            eq_(res.status_code, 200)
            eq_(json.loads(res.content)['app_threads'],
                [{"id": thread2.id, "version__version": version2.version},
                 {"id": thread1.id, "version__version": version1.version}])


class TestThreadList(RestOAuth, CommTestMixin):
    fixtures = fixture('webapp_337141', 'user_2519')

    def setUp(self):
        super(TestThreadList, self).setUp()
        self.create_switch('comm-dashboard')
        self.addon = Webapp.objects.get(pk=337141)
        self.list_url = reverse('comm-thread-list')

    def test_response(self):
        """Test the list response, we don't want public threads in the list."""
        self._thread_factory(note=True, perms=['public'])

        res = self.client.get(self.list_url)
        eq_(res.status_code, 200)
        eq_(len(res.json['objects']), 1)

    def test_addon_filter(self):
        self._thread_factory(note=True)

        self.grant_permission(self.user, 'Apps:Review')
        res = self.client.get(self.list_url, {'app': '337141'})
        eq_(res.status_code, 200)
        eq_(len(res.json['objects']), 1)

        # This add-on doesn't exist.
        res = self.client.get(self.list_url, {'app': '1000'})
        eq_(res.status_code, 404)

    def test_app_slug(self):
        thread = CommunicationThread.objects.create(addon=self.addon)
        CommunicationNote.objects.create(author=self.profile, thread=thread,
            note_type=0, body='something')

        self.grant_permission(self.user, 'Apps:Review')
        res = self.client.get(self.list_url, {'app': self.addon.app_slug})
        eq_(res.status_code, 200)
        eq_(res.json['objects'][0]['addon_meta']['app_slug'],
            self.addon.app_slug)

    def test_app_threads(self):
        version1 = version_factory(addon=self.addon, version='7.12')
        thread1 = CommunicationThread.objects.create(
            addon=self.addon, version=version1, read_permission_public=True)
        CommunicationThreadCC.objects.create(user=self.profile, thread=thread1)

        version2 = version_factory(addon=self.addon, version='1.16')
        thread2 = CommunicationThread.objects.create(
            addon=self.addon, version=version2, read_permission_public=True)
        CommunicationThreadCC.objects.create(user=self.profile, thread=thread2)

        self.grant_permission(self.user, 'Apps:Review')
        res = self.client.get(self.list_url, {'app': self.addon.app_slug})
        eq_(res.status_code, 200)
        eq_(res.json['app_threads'],
            [{'id': thread2.id, 'version__version': version2.version},
             {'id': thread1.id, 'version__version': version1.version}])

    def test_create(self):
        self.create_switch('comm-dashboard')
        version_factory(addon=self.addon, version='1.1')
        data = {
            'app': self.addon.app_slug,
            'version': '1.1',
            'note_type': '0',
            'body': 'flylikebee'
        }
        self.addon.addonuser_set.create(user=self.user)
        res = self.client.post(self.list_url, data=json.dumps(data))
        eq_(res.status_code, 201)
        assert self.addon.threads.count()


class NoteSetupMixin(RestOAuth, CommTestMixin, AttachmentManagementMixin):
    fixtures = fixture('webapp_337141', 'user_2519', 'user_999',
                       'user_support_staff')

    def setUp(self):
        super(NoteSetupMixin, self).setUp()
        self.create_switch('comm-dashboard')

        self.addon = Webapp.objects.get(pk=337141)
        self.thread = self._thread_factory(
            perms=['developer'], version=self.addon.current_version)
        self.thread_url = reverse(
            'comm-thread-detail', kwargs={'pk': self.thread.id})
        self.list_url = reverse(
            'comm-note-list', kwargs={'thread_id': self.thread.id})

        self.profile.addonuser_set.create(addon=self.addon)


class TestNote(NoteSetupMixin):

    @override_settings(REVIEWER_ATTACHMENTS_PATH=TESTS_DIR)
    def test_response(self):
        note = self._note_factory(self.thread)
        attach = note.attachments.create(filepath='test_views.py',
                                         description='desc')

        res = self.client.get(reverse(
            'comm-note-detail',
            kwargs={'thread_id': self.thread.id, 'pk': note.id}))
        eq_(res.status_code, 200)
        eq_(res.json['body'], 'something')

        # Attachments.
        eq_(len(res.json['attachments']), 1)
        eq_(res.json['attachments'][0]['url'],
            settings.SITE_URL +
            reverse('comm-attachment-detail', args=[note.id, attach.id]))
        eq_(res.json['attachments'][0]['display_name'], 'desc')
        ok_(not res.json['attachments'][0]['is_image'])

    def test_read_perms(self):
        staff = UserProfile.objects.get(username='support_staff')
        self._note_factory(
            self.thread, perms=['developer'], author=staff, body='oncetoldme')
        no_dev_note = self._note_factory(
            self.thread, no_perms=['developer'], author=staff)

        res = self.client.get(self.list_url)
        eq_(res.status_code, 200)
        eq_(len(res.json['objects']), 1)
        eq_(res.json['objects'][0]['body'], 'oncetoldme')

        # Test that the author always has permissions.
        no_dev_note.update(author=self.profile)
        res = self.client.get(self.list_url)
        eq_(len(res.json['objects']), 2)

    def test_create(self):
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': '0', 'body': 'something'}))
        eq_(res.status_code, 201)
        eq_(res.json['body'], 'something')

    def test_create_dev_comment(self):
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': comm.DEVELOPER_COMMENT,
                                'body': 'something'}))
        eq_(res.status_code, 201)

        self.addon.addonuser_set.filter(user=self.profile).delete()
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': comm.DEVELOPER_COMMENT,
                                'body': 'something'}))
        eq_(res.status_code, 403)

    def test_create_rev_comment(self):
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': comm.REVIEWER_COMMENT,
                                'body': 'something'}))
        eq_(res.status_code, 403)

        self.grant_permission(self.profile, 'Apps:Review')
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': comm.DEVELOPER_COMMENT,
                                'body': 'something'}))
        eq_(res.status_code, 201)

    def test_create_whitelisted_note_types(self):
        res = self.client.post(self.list_url, data=json.dumps(
                               {'note_type': comm.RESUBMISSION,
                                'body': 'something'}))
        eq_(res.status_code, 400)

    def test_create_no_perm(self):
        self.thread.update(read_permission_developer=False)
        res = self.client.post(self.list_url, data=json.dumps(
            {'note_type': '0', 'body': 'something'}))
        eq_(res.status_code, 403)

    def test_cors_allowed(self):
        res = self.client.get(self.list_url)
        self.assertCORS(res, 'get', 'post', 'patch')


@override_settings(REVIEWER_ATTACHMENTS_PATH=ATTACHMENTS_DIR)
class TestAttachments(NoteSetupMixin):

    def setUp(self):
        super(TestAttachments, self).setUp()
        self.note = self._note_factory(self.thread, author=self.profile)
        self.attachment_url = reverse(
            'comm-attachment-list', kwargs={'note_id': self.note.id})

    def test_cors_bad_request(self):
        res = self.client.post(self.attachment_url, data={},
                               content_type=MULTIPART_CONTENT)
        eq_(res.status_code, 400)
        self.assertCORS(res, 'get', 'post')

    def _save_attachment_mock(self, storage, attachment, filepath):
        if 'jpg' in filepath:
            return 'bacon.jpg'
        return 'bacon.txt'

    @mock.patch('mkt.comm.utils._save_attachment')
    def test_create_attachment(self, _mock):
        _mock.side_effect = self._save_attachment_mock

        data = self._attachments(num=2)
        res = self.client.post(self.attachment_url, data=data,
                               content_type=MULTIPART_CONTENT)

        eq_(res.status_code, 201)
        eq_(CommAttachment.objects.count(), 2)

        attach1 = CommAttachment.objects.all()[0]
        eq_(attach1.note, self.note)
        eq_(attach1.filepath, 'bacon.txt')
        eq_(attach1.description, '')
        assert not attach1.is_image()

        attach2 = CommAttachment.objects.all()[1]
        eq_(attach2.note, self.note)
        eq_(attach2.filepath, 'bacon.jpg')
        eq_(attach2.description, 'mmm, bacon')
        assert attach2.is_image()

    @mock.patch.object(comm, 'MAX_ATTACH', 1)
    def test_max_attach(self):
        data = self._attachments(num=2)
        res = self.client.post(self.attachment_url, data=data,
                               content_type=MULTIPART_CONTENT)
        eq_(res.status_code, 400)

    def test_not_note_owner(self):
        self.note.update(author=user_factory())
        data = self._attachments(num=2)
        res = self.client.post(self.attachment_url, data=data,
                               content_type=MULTIPART_CONTENT)

        eq_(res.status_code, 403)

    @mock.patch('mkt.comm.utils._save_attachment', new=mock.Mock())
    @mock.patch('mkt.comm.models.CommAttachment.is_image', new=mock.Mock())
    def test_get_attachment(self):
        if not settings.XSENDFILE:
            raise SkipTest

        data = self._attachments(num=1)
        res = self.client.post(self.attachment_url, data=data,
                               content_type=MULTIPART_CONTENT)
        attachment_id = res.json['attachments'][0]['id']

        get_attachment_url = reverse('comm-attachment-detail',
                                     args=[self.note.id, attachment_id])
        res = self.client.get(get_attachment_url)
        eq_(res.status_code, 200)
        eq_(res._headers['x-sendfile'][1],
            CommAttachment.objects.get(id=attachment_id).full_path())

    @mock.patch('mkt.comm.utils._save_attachment', new=mock.Mock())
    @mock.patch('mkt.comm.models.CommAttachment.is_image', new=mock.Mock())
    def test_get_attachment_not_note_perm(self):
        data = self._attachments(num=1)
        res = self.client.post(self.attachment_url, data=data,
                               content_type=MULTIPART_CONTENT)
        attachment_id = res.json['attachments'][0]['id']

        # Remove perms.
        self.note.update(author=user_factory())
        self.profile.addonuser_set.all().delete()
        get_attachment_url = reverse('comm-attachment-detail',
                                     args=[self.note.id, attachment_id])
        res = self.client.get(get_attachment_url)
        eq_(res.status_code, 403)


@mock.patch.object(settings, 'WHITELISTED_CLIENTS_EMAIL_API',
                   ['10.10.10.10'])
@mock.patch.object(settings, 'POSTFIX_AUTH_TOKEN', 'something')
class TestEmailApi(RestOAuth):

    def get_request(self, data=None):
        req = req_factory_factory(reverse('post-email-api'), self.profile)
        req.META['REMOTE_ADDR'] = '10.10.10.10'
        req.META['HTTP_POSTFIX_AUTH_TOKEN'] = 'something'
        req.POST = data or {}
        req.method = 'POST'
        return req

    def test_basic(self):
        sample_email = os.path.join(settings.ROOT, 'mkt', 'comm', 'tests',
                                    'email.txt')
        req = self.get_request(data={'body': open(sample_email).read()})

        app = app_factory()
        user = user_factory()
        self.grant_permission(user, 'Admin:*')
        t = CommunicationThread.objects.create(addon=app,
                                               version=app.current_version)
        t.token.create(user=user, uuid='5a0b8a83d501412589cc5d562334b46b')

        res = post_email(req)
        eq_(res.status_code, 201)
        ok_(t.notes.count())

    def test_allowed(self):
        assert EmailCreationPermission().has_permission(self.get_request(),
                                                        None)

    def test_ip_denied(self):
        req = self.get_request()
        req.META['REMOTE_ADDR'] = '10.10.10.1'
        assert not EmailCreationPermission().has_permission(req, None)

    def test_token_denied(self):
        req = self.get_request()
        req.META['HTTP_POSTFIX_AUTH_TOKEN'] = 'somethingwrong'
        assert not EmailCreationPermission().has_permission(req, None)

    @mock.patch('mkt.comm.tasks.consume_email.apply_async')
    def test_successful(self, _mock):
        req = self.get_request({'body': 'something'})
        res = post_email(req)
        _mock.assert_called_with(('something',))
        eq_(res.status_code, 201)

    def test_bad_request(self):
        """Test with no email body."""
        res = post_email(self.get_request())
        eq_(res.status_code, 400)


class TestCommCC(RestOAuth, CommTestMixin):
    fixtures = fixture('webapp_337141', 'user_2519', 'user_support_staff')

    def setUp(self):
        super(TestCommCC, self).setUp()
        self.addon = Webapp.objects.get(pk=337141)
        self.profile = UserProfile.objects.get(id=2519)

    def test_delete(self):
        thread = self._thread_factory()
        ok_(thread.thread_cc.create(user=self.profile))
        res = self.client.delete(
            reverse('comm-thread-cc-detail', args=[thread.id]))
        eq_(res.status_code, 204)
        eq_(CommunicationThreadCC.objects.count(), 0)
