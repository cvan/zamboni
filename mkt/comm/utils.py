import base64
import logging
import urllib2
from email import message_from_string
from email.utils import parseaddr

from django.conf import settings
from django.core.files.storage import get_storage_class
from django.core.urlresolvers import reverse
from django.utils import translation

import waffle
from email_reply_parser import EmailReplyParser

import amo
from amo.utils import to_language

from mkt.access import acl
from mkt.access.models import Group
from mkt.comm.models import (CommunicationNoteRead, CommunicationThreadToken,
                             user_has_perm_thread)
from mkt.constants import comm
from mkt.site.helpers import absolutify
from mkt.site.mail import send_mail_jinja
from mkt.users.models import UserProfile
from mkt.webapps.models import Webapp


log = logging.getLogger('z.comm')


class CommEmailParser(object):
    """Utility to parse email replies."""
    address_prefix = comm.REPLY_TO_PREFIX

    def __init__(self, email_text):
        """Decode base64 email and turn it into a Django email object."""
        try:
            log.info('CommEmailParser received email: ' + email_text)
            email_text = base64.standard_b64decode(
                urllib2.unquote(email_text.rstrip()))
        except TypeError:
            # Corrupt or invalid base 64.
            self.decode_error = True
            log.info('Decoding error for CommEmailParser')
            return

        self.email = message_from_string(email_text)

        payload = self.email.get_payload()  # If not multipart, it's a string.
        if isinstance(payload, list):
            # If multipart, get the plaintext part.
            for part in payload:
                if part.get_content_type() == 'text/plain':
                    payload = part.get_payload()
                    break

        self.reply_text = EmailReplyParser.read(payload).reply

    def _get_address_line(self):
        return parseaddr(self.email['to'])

    def get_uuid(self):
        name, addr = self._get_address_line()

        if addr.startswith(self.address_prefix):
            # Strip everything between "reply+" and the "@" sign.
            uuid = addr[len(self.address_prefix):].split('@')[0]
        else:
            log.info('TO: address missing or not related to comm. (%s)'
                      % unicode(self.email).strip())
            return False

        return uuid

    def get_body(self):
        return self.reply_text


def save_from_email_reply(reply_text):
    log.debug("Saving from email reply")

    parser = CommEmailParser(reply_text)
    if hasattr(parser, 'decode_error'):
        return False

    uuid = parser.get_uuid()

    if not uuid:
        return False
    try:
        tok = CommunicationThreadToken.objects.get(uuid=uuid)
    except CommunicationThreadToken.DoesNotExist:
        log.error('An email was skipped with non-existing uuid %s.' % uuid)
        return False

    if user_has_perm_thread(tok.thread, tok.user) and tok.is_valid():
        # Deduce an appropriate note type.
        note_type = comm.NO_ACTION
        if (tok.user.addonuser_set.filter(addon=tok.thread.addon).exists()):
            note_type = comm.DEVELOPER_COMMENT
        elif acl.action_allowed_user(tok.user, 'Apps', 'Review'):
            note_type = comm.REVIEWER_COMMENT

        t, note = create_comm_note(tok.thread.addon, tok.thread.version,
                                   tok.user, parser.get_body(),
                                   note_type=note_type)
        log.info('A new note has been created (from %s using tokenid %s).'
                 % (tok.user.id, uuid))
        return note
    elif tok.is_valid():
        log.error('%s did not have perms to reply to comm email thread %s.'
                  % (tok.user.email, tok.thread.id))
    else:
        log.error('%s tried to use an invalid comm token for thread %s.'
                  % (tok.user.email, tok.thread.id))

    return False


def filter_notes_by_read_status(queryset, profile, read_status=True):
    """
    Filter read/unread notes using this method.

    `read_status` = `True` for read notes, `False` for unread notes.
    """
    # Get some read notes from db.
    notes = list(CommunicationNoteRead.objects.filter(
        user=profile).values_list('note', flat=True))

    if read_status:
        # Filter and return read notes if they exist.
        return queryset.filter(pk__in=notes) if notes else queryset.none()
    else:
        # Exclude read notes if they exist.
        return queryset.exclude(pk__in=notes) if notes else queryset.all()


def get_reply_token(thread, user_id):
    tok, created = CommunicationThreadToken.objects.get_or_create(
        thread=thread, user_id=user_id)

    # We expire a token after it has been used for a maximum number of times.
    # This is usually to prevent overusing a single token to spam to threads.
    # Since we're re-using tokens, we need to make sure they are valid for
    # replying to new notes so we reset their `use_count`.
    if not created:
        tok.update(use_count=0)
    else:
        log.info('Created token with UUID %s for user_id: %s.' %
                 (tok.uuid, user_id))
    return tok


def get_recipients(note):
    """
    Determine email recipients based on a new note based on those who are on
    the thread_cc list and note permissions.
    Returns reply-to-tokenized emails.
    """
    thread = note.thread
    recipients = []

    # Whitelist: include recipients.
    if note.note_type == comm.ESCALATION:
        # Email only senior reviewers on escalations.
        seniors = Group.objects.get(name='Senior App Reviewers')
        recipients = seniors.users.values_list('id', 'email')
    else:
        # Get recipients via the CommunicationThreadCC table, which is usually
        # populated with the developer, the Mozilla contact, and anyone that
        # posts to and reviews the app.
        recipients = set(thread.thread_cc.values_list(
            'user__id', 'user__email'))

    # Blacklist: exclude certain people from receiving the email based on
    # permission.
    excludes = []
    if not note.read_permission_developer:
        # Exclude developer.
        excludes += thread.addon.authors.values_list('id', 'email')

    if note.author:
        # Exclude note author.
        excludes.append((note.author.id, note.author.email))

    # Remove excluded people from the recipients.
    recipients = [r for r in recipients if r not in excludes]

    # Build reply-to-tokenized email addresses.
    new_recipients_list = []
    for user_id, user_email in recipients:
        tok = get_reply_token(note.thread, user_id)
        new_recipients_list.append((user_email, tok.uuid))

    return new_recipients_list


def get_mail_context(note):
    """
    Get context data for comm emails, specifically for review action emails.
    """
    app = note.thread.addon

    if app.name.locale != app.default_locale:
        # We need to display the name in some language that is relevant to the
        # recipient(s) instead of using the reviewer's. addon.default_locale
        # should work.
        lang = to_language(app.default_locale)
        with translation.override(lang):
            app = Webapp.objects.get(id=app.id)

    return {
        'amo': amo,
        'app': app,
        'comm': comm,
        'comments': note.body,
        'detail_url': absolutify(
            app.get_url_path(add_prefix=False)),
        'MKT_SUPPORT_EMAIL': settings.MKT_SUPPORT_EMAIL,
        'name': app.name,
        'note': note,
        'review_url': absolutify(reverse('reviewers.apps.review',
                                 args=[app.app_slug], add_prefix=False)),
        'reviewer': note.author,
        'sender': note.author.name if note.author else 'System',
        'SITE_URL': settings.SITE_URL,
        'status_url': absolutify(app.get_dev_url('versions')),
        'thread_id': str(note.thread.id)
    }


def send_mail_comm(note):
    """
    Email utility used globally by the Communication Dashboard to send emails.
    Given a note (its actions and permissions), recipients are determined and
    emails are sent to appropriate people.
    """
    from mkt.reviewers.utils import send_reviewer_mail

    if not waffle.switch_is_active('comm-dashboard'):
        return

    recipients = get_recipients(note)
    name = note.thread.addon.name
    subject = '%s: %s' % (unicode(comm.NOTE_TYPES[note.note_type]), name)

    log.info(u'Sending emails for %s' % note.thread.addon)
    for email, tok in recipients:
        reply_to = '{0}{1}@{2}'.format(comm.REPLY_TO_PREFIX, tok,
                                       settings.POSTFIX_DOMAIN)

        # Get the appropriate mail template.
        mail_template = comm.COMM_MAIL_MAP.get(note.note_type, 'generic')
        # Send mail.
        send_mail_jinja(subject, 'comm/emails/%s.html' % mail_template,
                        get_mail_context(note), recipient_list=[email],
                        from_email=settings.MKT_REVIEWERS_EMAIL,
                        perm_setting='app_reviewed',
                        headers={'reply_to': reply_to})


def create_comm_note(app, version, author, body, note_type=comm.NO_ACTION,
                     perms=None, attachments=None):
    """
    Creates a note on an app version's thread.
    Creates a thread if a thread doesn't already exist.
    CC's app's Mozilla contacts to auto-join thread.

    app -- app object.
    version -- app version.
    author -- UserProfile for the note's author.
    body -- string/text for note comment.
    note_type -- integer for note_type (mkt constant), defaults to 0/NO_ACTION
                 (e.g. comm.APPROVAL, comm.REJECTION, comm.NO_ACTION).
    perms -- object of groups to grant permission to, will set flags on Thread.
             (e.g. {'developer': False, 'staff': True}).
    attachments -- formset of attachment files
    """
    # Perm for reviewer, senior_reviewer, moz_contact, staff True by default.
    # Perm for developer False if is reviewer-only comment by default.
    perms = perms or {}
    if 'developer' not in perms and note_type in comm.REVIEWER_NOTE_TYPES:
        perms['developer'] = False
    create_perms = dict(('read_permission_%s' % key, has_perm)
                        for key, has_perm in perms.iteritems())

    # Create thread + note.
    thread, created_thread = app.threads.safer_get_or_create(
        version=version, defaults=create_perms)
    note = thread.notes.create(
        note_type=note_type, body=body, author=author, **create_perms)

    if attachments:
        create_attachments(note, attachments)

    post_create_comm_note(note)

    return thread, note


def post_create_comm_note(note):
    """Stuff to do after creating note, also used in comm api's post_save."""
    thread = note.thread
    app = thread.addon

    # Add developer to thread.
    for developer in app.authors.all():
        thread.join_thread(developer)

    # Add Mozilla contact to thread.
    for email in app.get_mozilla_contacts():
        try:
            moz_contact = UserProfile.objects.get(email=email)
            thread.join_thread(moz_contact)
        except UserProfile.DoesNotExist:
            pass

    # Add note author to thread.
    author = note.author
    if author:
        cc, created_cc = thread.join_thread(author)
        if not created_cc:
            # Mark their own note as read.
            note.mark_read(note.author)

    # Send out emails.
    send_mail_comm(note)


def create_attachments(note, formset):
    """Create attachments from CommAttachmentFormSet onto note."""
    errors = []
    storage = get_storage_class()()

    for form in formset:
        if not form.is_valid():
            errors.append(form.errors)
            continue

        data = form.cleaned_data
        if not data:
            continue

        attachment = data['attachment']
        attachment_name = _save_attachment(
            storage, attachment,
            '%s/%s' % (settings.REVIEWER_ATTACHMENTS_PATH, attachment.name))

        note.attachments.create(
            description=data.get('description'), filepath=attachment_name,
            mimetype=attachment.content_type)

    return errors


def _save_attachment(storage, attachment, filepath):
    """Saves an attachment and returns the filename."""
    filepath = storage.save(filepath, attachment)
    # In case of duplicate filename, storage suffixes filename.
    return filepath.split('/')[-1]
