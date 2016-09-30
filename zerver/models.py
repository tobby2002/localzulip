from __future__ import absolute_import
from typing import Any, List, Set, Tuple, TypeVar, \
    Union, Optional, Sequence, AbstractSet
from typing.re import Match
from zerver.lib.str_utils import NonBinaryStr

from django.db import models
from django.db.models.query import QuerySet
from django.db.models import Manager
from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, UserManager, \
    PermissionsMixin
from django.dispatch import receiver
from zerver.lib.cache import cache_with_key, flush_user_profile, flush_realm, \
    user_profile_by_id_cache_key, user_profile_by_email_cache_key, \
    generic_bulk_cached_fetch, cache_set, flush_stream, \
    display_recipient_cache_key, cache_delete, \
    get_stream_cache_key, active_user_dicts_in_realm_cache_key, \
    active_bot_dicts_in_realm_cache_key, active_user_dict_fields, \
    active_bot_dict_fields
from zerver.lib.utils import make_safe_digest, generate_random_token
from zerver.lib.str_utils import force_bytes, ModelReprMixin, dict_with_str_keys
from django.db import transaction
from zerver.lib.avatar import gravatar_hash, get_avatar_url
from zerver.lib.camo import get_camo_url
from django.utils import timezone
from django.contrib.sessions.models import Session
from zerver.lib.timestamp import datetime_to_timestamp
from django.db.models.signals import pre_save, post_save, post_delete
from django.core.validators import MinLengthValidator, RegexValidator
from django.utils.translation import ugettext_lazy as _
import zlib

from bitfield import BitField
from bitfield.types import BitHandler
from collections import defaultdict
from datetime import timedelta
import pylibmc
import re
import ujson
import logging
from six import binary_type, text_type
import time
import datetime

# TODO: see #1379 to eliminate bugdown dependencies
bugdown = None # type: Any

MAX_SUBJECT_LENGTH = 60
MAX_MESSAGE_LENGTH = 10000
MAX_LANGUAGE_ID_LENGTH = 50 # type: int

STREAM_NAMES = TypeVar('STREAM_NAMES', Sequence[text_type], AbstractSet[text_type])

# Doing 1000 remote cache requests to get_display_recipient is quite slow,
# so add a local cache as well as the remote cache cache.
per_request_display_recipient_cache = {} # type: Dict[int, List[Dict[str, Any]]]
def get_display_recipient_by_id(recipient_id, recipient_type, recipient_type_id):
    # type: (int, int, int) -> Union[text_type, List[Dict[str, Any]]]
    if recipient_id not in per_request_display_recipient_cache:
        result = get_display_recipient_remote_cache(recipient_id, recipient_type, recipient_type_id)
        per_request_display_recipient_cache[recipient_id] = result
    return per_request_display_recipient_cache[recipient_id]

def get_display_recipient(recipient):
    # type: (Recipient) -> Union[text_type, List[Dict[str, Any]]]
    return get_display_recipient_by_id(
            recipient.id,
            recipient.type,
            recipient.type_id
    )

def flush_per_request_caches():
    # type: () -> None
    global per_request_display_recipient_cache
    per_request_display_recipient_cache = {}
    global per_request_realm_filters_cache
    per_request_realm_filters_cache = {}

@cache_with_key(lambda *args: display_recipient_cache_key(args[0]),
                timeout=3600*24*7)
def get_display_recipient_remote_cache(recipient_id, recipient_type, recipient_type_id):
    # type: (int, int, int) -> Union[text_type, List[Dict[str, Any]]]
    """
    returns: an appropriate object describing the recipient.  For a
    stream this will be the stream name as a string.  For a huddle or
    personal, it will be an array of dicts about each recipient.
    """
    if recipient_type == Recipient.STREAM:
        stream = Stream.objects.get(id=recipient_type_id)
        return stream.name

    # We don't really care what the ordering is, just that it's deterministic.
    user_profile_list = (UserProfile.objects.filter(subscription__recipient_id=recipient_id)
                                            .select_related()
                                            .order_by('email'))
    return [{'email': user_profile.email,
             'domain': user_profile.realm.domain,
             'full_name': user_profile.full_name,
             'short_name': user_profile.short_name,
             'id': user_profile.id,
             'is_mirror_dummy': user_profile.is_mirror_dummy,} for user_profile in user_profile_list]

def completely_open(domain):
    # type: (text_type) -> bool
    # This domain is completely open to everyone on the internet to
    # join. E-mail addresses do not need to match the domain and
    # an invite from an existing user is not required.
    realm = get_realm(domain)
    if not realm:
        return False
    return not realm.invite_required and not realm.restricted_to_domain

def get_unique_open_realm():
    # type: () -> Optional[Realm]
    """We only return a realm if there is a unique non-system-only realm
    and it is completely open."""
    realms = Realm.objects.filter(deactivated=False)
    # On production installations, the (usually "zulip.com") system
    # realm is an empty realm just used for system bots, so don't
    # include it in this accounting.
    realms = realms.exclude(domain__in=settings.SYSTEM_ONLY_REALMS)
    if len(realms) != 1:
        return None
    realm = realms[0]
    if realm.invite_required or realm.restricted_to_domain:
        return None
    return realm

def get_realm_emoji_cache_key(realm):
    # type: (Realm) -> text_type
    return u'realm_emoji:%s' % (realm.id,)

class Realm(ModelReprMixin, models.Model):
    # domain is a domain in the Internet sense. It must be structured like a
    # valid email domain. We use is to restrict access, identify bots, etc.
    domain = models.CharField(max_length=40, db_index=True, unique=True) # type: text_type
    # name is the user-visible identifier for the realm. It has no required
    # structure.
    name = models.CharField(max_length=40, null=True) # type: Optional[text_type]
    restricted_to_domain = models.BooleanField(default=True) # type: bool
    invite_required = models.BooleanField(default=False) # type: bool
    invite_by_admins_only = models.BooleanField(default=False) # type: bool
    create_stream_by_admins_only = models.BooleanField(default=False) # type: bool
    mandatory_topics = models.BooleanField(default=False) # type: bool
    show_digest_email = models.BooleanField(default=True) # type: bool
    name_changes_disabled = models.BooleanField(default=False) # type: bool

    allow_message_editing = models.BooleanField(default=True) # type: bool
    DEFAULT_MESSAGE_CONTENT_EDIT_LIMIT_SECONDS = 600 # if changed, also change in admin.js
    message_content_edit_limit_seconds = models.IntegerField(default=DEFAULT_MESSAGE_CONTENT_EDIT_LIMIT_SECONDS) # type: int

    date_created = models.DateTimeField(default=timezone.now) # type: datetime.datetime
    notifications_stream = models.ForeignKey('Stream', related_name='+', null=True, blank=True) # type: Optional[Stream]
    deactivated = models.BooleanField(default=False) # type: bool
    default_language = models.CharField(default=u'en', max_length=MAX_LANGUAGE_ID_LENGTH) # type: text_type

    DEFAULT_NOTIFICATION_STREAM_NAME = u'announce'

    def __unicode__(self):
        # type: () -> text_type
        return u"<Realm: %s %s>" % (self.domain, self.id)

    @cache_with_key(get_realm_emoji_cache_key, timeout=3600*24*7)
    def get_emoji(self):
        # type: () -> Dict[text_type, Dict[str, text_type]]
        return get_realm_emoji_uncached(self)

    @property
    def deployment(self):
        # type: () -> Any # returns a Deployment from zilencer.models
        try:
            return self._deployments.all()[0]
        except IndexError:
            return None

    @deployment.setter # type: ignore # https://github.com/python/mypy/issues/220
    def set_deployments(self, value):
        # type: (Any) -> None
        self._deployments = [value] # type: Any

    def get_admin_users(self):
        # type: () -> Sequence[UserProfile]
        # TODO: Change return type to QuerySet[UserProfile]
        return UserProfile.objects.filter(realm=self, is_realm_admin=True,
                                          is_active=True).select_related()

    def get_active_users(self):
        # type: () -> Sequence[UserProfile]
        # TODO: Change return type to QuerySet[UserProfile]
        return UserProfile.objects.filter(realm=self, is_active=True).select_related()

    @property
    def uri(self):
        # type: () -> str
        return settings.SERVER_URI

    @property
    def host(self):
        # type: () -> str
        return settings.EXTERNAL_HOST

    @property
    def is_zephyr_mirror_realm(self):
        # type: () -> bool
        return self.domain == "mit.edu"

    @property
    def webathena_enabled(self):
        # type: () -> bool
        return self.is_zephyr_mirror_realm

    @property
    def presence_disabled(self):
        # type: () -> bool
        return self.is_zephyr_mirror_realm

    class Meta(object):
        permissions = (
            ('administer', "Administer a realm"),
            ('api_super_user', "Can send messages as other users for mirroring"),
        )

post_save.connect(flush_realm, sender=Realm)

class RealmAlias(models.Model):
    realm = models.ForeignKey(Realm, null=True) # type: Optional[Realm]
    domain = models.CharField(max_length=80, db_index=True, unique=True) # type: text_type

# These functions should only be used on email addresses that have
# been validated via django.core.validators.validate_email
#
# Note that we need to use some care, since can you have multiple @-signs; e.g.
# "tabbott@test"@zulip.com
# is valid email address
def email_to_username(email):
    # type: (text_type) -> text_type
    return "@".join(email.split("@")[:-1]).lower()

# Returns the raw domain portion of the desired email address
def split_email_to_domain(email):
    # type: (text_type) -> text_type
    return email.split("@")[-1].lower()

# Returns the domain, potentually de-aliased, for the realm
# that this user's email is in
def resolve_email_to_domain(email):
    # type: (text_type) -> text_type
    domain = split_email_to_domain(email)
    alias = alias_for_realm(domain)
    if alias is not None:
        domain = alias.realm.domain
    return domain

# Is a user with the given email address allowed to be in the given realm?
# (This function does not check whether the user has been invited to the realm.
# So for invite-only realms, this is the test for whether a user can be invited,
# not whether the user can sign up currently.)
def email_allowed_for_realm(email, realm):
    # type: (text_type, Realm) -> bool
    # Anyone can be in an open realm
    if not realm.restricted_to_domain:
        return True

    # Otherwise, domains must match (case-insensitively)
    email_domain = resolve_email_to_domain(email)
    return email_domain == realm.domain.lower()

def alias_for_realm(domain):
    # type: (text_type) -> Optional[RealmAlias]
    try:
        return RealmAlias.objects.get(domain=domain)
    except RealmAlias.DoesNotExist:
        return None

def remote_user_to_email(remote_user):
    # type: (text_type) -> text_type
    if settings.SSO_APPEND_DOMAIN is not None:
        remote_user += "@" + settings.SSO_APPEND_DOMAIN
    return remote_user

class RealmEmoji(ModelReprMixin, models.Model):
    realm = models.ForeignKey(Realm) # type: Realm
    # Second part of the regex (negative lookbehind) disallows names ending with one of the punctuation characters
    name = models.TextField(validators=[MinLengthValidator(1),
                                        RegexValidator(regex=r'^[0-9a-zA-Z.\-_]+(?<![.\-_])$',
                                                       message=_("Invalid characters in Emoji name"))]) # type: text_type
    # URLs start having browser compatibility problem below 2000
    # characters, so 1000 seems like a safe limit.
    img_url = models.URLField(max_length=1000) # type: text_type

    class Meta(object):
        unique_together = ("realm", "name")

    def __unicode__(self):
        # type: () -> text_type
        return u"<RealmEmoji(%s): %s %s>" % (self.realm.domain, self.name, self.img_url)

def get_realm_emoji_uncached(realm):
    # type: (Realm) -> Dict[text_type, Dict[str, text_type]]
    d = {}
    for row in RealmEmoji.objects.filter(realm=realm):
        d[row.name] = dict(source_url=row.img_url,
                           display_url=get_camo_url(row.img_url))
    return d

def flush_realm_emoji(sender, **kwargs):
    # type: (Any, **Any) -> None
    realm = kwargs['instance'].realm
    cache_set(get_realm_emoji_cache_key(realm),
              get_realm_emoji_uncached(realm),
              timeout=3600*24*7)

post_save.connect(flush_realm_emoji, sender=RealmEmoji)
post_delete.connect(flush_realm_emoji, sender=RealmEmoji)

class RealmFilter(ModelReprMixin, models.Model):
    realm = models.ForeignKey(Realm) # type: Realm
    pattern = models.TextField() # type: text_type
    url_format_string = models.TextField() # type: text_type

    class Meta(object):
        unique_together = ("realm", "pattern")

    def __unicode__(self):
        # type: () -> text_type
        return u"<RealmFilter(%s): %s %s>" % (self.realm.domain, self.pattern, self.url_format_string)

def get_realm_filters_cache_key(domain):
    # type: (text_type) -> text_type
    return u'all_realm_filters:%s' % (domain,)

# We have a per-process cache to avoid doing 1000 remote cache queries during page load
per_request_realm_filters_cache = {} # type: Dict[text_type, List[Tuple[text_type, text_type]]]
def realm_filters_for_domain(domain):
    # type: (text_type) -> List[Tuple[text_type, text_type]]
    domain = domain.lower()
    if domain not in per_request_realm_filters_cache:
        per_request_realm_filters_cache[domain] = realm_filters_for_domain_remote_cache(domain)
    return per_request_realm_filters_cache[domain]

@cache_with_key(get_realm_filters_cache_key, timeout=3600*24*7)
def realm_filters_for_domain_remote_cache(domain):
    # type: (text_type) -> List[Tuple[text_type, text_type]]
    filters = []
    for realm_filter in RealmFilter.objects.filter(realm=get_realm(domain)):
       filters.append((realm_filter.pattern, realm_filter.url_format_string))

    return filters

def all_realm_filters():
    # type: () -> Dict[text_type, List[Tuple[text_type, text_type]]]
    filters = defaultdict(list) # type: Dict[text_type, List[Tuple[text_type, text_type]]]
    for realm_filter in RealmFilter.objects.all():
       filters[realm_filter.realm.domain].append((realm_filter.pattern, realm_filter.url_format_string))

    return filters

def flush_realm_filter(sender, **kwargs):
    # type: (Any, **Any) -> None
    realm = kwargs['instance'].realm
    cache_delete(get_realm_filters_cache_key(realm.domain))
    try:
        per_request_realm_filters_cache.pop(realm.domain.lower())
    except KeyError:
        pass

post_save.connect(flush_realm_filter, sender=RealmFilter)
post_delete.connect(flush_realm_filter, sender=RealmFilter)

class UserProfile(ModelReprMixin, AbstractBaseUser, PermissionsMixin):
    DEFAULT_BOT = 1
    """
    Incoming webhook bots are limited to only sending messages via webhooks.
    Thus, it is less of a security risk to expose their API keys to third-party services,
    since they can't be used to read messages.
    """
    INCOMING_WEBHOOK_BOT = 2

    # Fields from models.AbstractUser minus last_name and first_name,
    # which we don't use; email is modified to make it indexed and unique.
    email = models.EmailField(blank=False, db_index=True, unique=True) # type: text_type
    is_staff = models.BooleanField(default=False) # type: bool
    is_active = models.BooleanField(default=True, db_index=True) # type: bool
    is_realm_admin = models.BooleanField(default=False, db_index=True) # type: bool
    is_bot = models.BooleanField(default=False, db_index=True) # type: bool
    bot_type = models.PositiveSmallIntegerField(null=True, db_index=True) # type: Optional[int]
    is_api_super_user = models.BooleanField(default=False, db_index=True) # type: bool
    date_joined = models.DateTimeField(default=timezone.now) # type: datetime.datetime
    is_mirror_dummy = models.BooleanField(default=False) # type: bool
    bot_owner = models.ForeignKey('self', null=True, on_delete=models.SET_NULL) # type: Optional[UserProfile]

    USERNAME_FIELD = 'email'
    MAX_NAME_LENGTH = 100

    # Our custom site-specific fields
    full_name = models.CharField(max_length=MAX_NAME_LENGTH) # type: text_type
    short_name = models.CharField(max_length=MAX_NAME_LENGTH) # type: text_type
    # pointer points to Message.id, NOT UserMessage.id.
    pointer = models.IntegerField() # type: int
    last_pointer_updater = models.CharField(max_length=64) # type: text_type
    realm = models.ForeignKey(Realm) # type: Realm
    api_key = models.CharField(max_length=32) # type: text_type
    tos_version = models.CharField(null=True, max_length=10) # type: text_type

    ### Notifications settings. ###

    # Stream notifications.
    enable_stream_desktop_notifications = models.BooleanField(default=False) # type: bool
    enable_stream_sounds = models.BooleanField(default=False) # type: bool

    # PM + @-mention notifications.
    enable_desktop_notifications = models.BooleanField(default=True) # type: bool
    enable_sounds = models.BooleanField(default=True) # type: bool
    enable_offline_email_notifications = models.BooleanField(default=True) # type: bool
    enable_offline_push_notifications = models.BooleanField(default=True) # type: bool

    enable_digest_emails = models.BooleanField(default=True) # type: bool

    # Old notification field superseded by existence of stream notification
    # settings.
    default_desktop_notifications = models.BooleanField(default=True) # type: bool

    ###

    last_reminder = models.DateTimeField(default=timezone.now, null=True) # type: Optional[datetime.datetime]
    rate_limits = models.CharField(default=u"", max_length=100) # type: text_type # comma-separated list of range:max pairs

    # Default streams
    default_sending_stream = models.ForeignKey('zerver.Stream', null=True, related_name='+') # type: Optional[Stream]
    default_events_register_stream = models.ForeignKey('zerver.Stream', null=True, related_name='+') # type: Optional[Stream]
    default_all_public_streams = models.BooleanField(default=False) # type: bool

    # UI vars
    enter_sends = models.NullBooleanField(default=True) # type: Optional[bool]
    autoscroll_forever = models.BooleanField(default=False) # type: bool
    left_side_userlist = models.BooleanField(default=False) # type: bool

    # display settings
    twenty_four_hour_time = models.BooleanField(default=False) # type: bool
    default_language = models.CharField(default=u'en', max_length=MAX_LANGUAGE_ID_LENGTH) # type: text_type

    # Hours to wait before sending another email to a user
    EMAIL_REMINDER_WAITPERIOD = 24
    # Minutes to wait before warning a bot owner that her bot sent a message
    # to a nonexistent stream
    BOT_OWNER_STREAM_ALERT_WAITPERIOD = 1

    AVATAR_FROM_GRAVATAR = u'G'
    AVATAR_FROM_USER = u'U'
    AVATAR_FROM_SYSTEM = u'S'
    AVATAR_SOURCES = (
            (AVATAR_FROM_GRAVATAR, 'Hosted by Gravatar'),
            (AVATAR_FROM_USER, 'Uploaded by user'),
            (AVATAR_FROM_SYSTEM, 'System generated'),
    )
    avatar_source = models.CharField(default=AVATAR_FROM_GRAVATAR, choices=AVATAR_SOURCES, max_length=1) # type: text_type

    TUTORIAL_WAITING  = u'W'
    TUTORIAL_STARTED  = u'S'
    TUTORIAL_FINISHED = u'F'
    TUTORIAL_STATES   = ((TUTORIAL_WAITING,  "Waiting"),
                         (TUTORIAL_STARTED,  "Started"),
                         (TUTORIAL_FINISHED, "Finished"))

    tutorial_status = models.CharField(default=TUTORIAL_WAITING, choices=TUTORIAL_STATES, max_length=1) # type: text_type
    # Contains serialized JSON of the form:
    #    [("step 1", true), ("step 2", false)]
    # where the second element of each tuple is if the step has been
    # completed.
    onboarding_steps = models.TextField(default=u'[]') # type: text_type

    invites_granted = models.IntegerField(default=0) # type: int
    invites_used = models.IntegerField(default=0) # type: int

    alert_words = models.TextField(default=u'[]') # type: text_type # json-serialized list of strings

    # Contains serialized JSON of the form:
    # [["social", "mit"], ["devel", "ios"]]
    muted_topics = models.TextField(default=u'[]') # type: text_type

    objects = UserManager() # type: UserManager

    def can_admin_user(self, target_user):
        # type: (UserProfile) -> bool
        """Returns whether this user has permission to modify target_user"""
        if target_user.bot_owner == self:
            return True
        elif self.is_realm_admin and self.realm == target_user.realm:
            return True
        else:
            return False

    def last_reminder_tzaware(self):
        # type: () -> Optional[datetime.datetime]
        if self.last_reminder is not None and timezone.is_naive(self.last_reminder):
            logging.warning(u"Loaded a user_profile.last_reminder for user %s that's not tz-aware: %s"
                              % (self.email, text_type(self.last_reminder)))
            return self.last_reminder.replace(tzinfo=timezone.utc)

        return self.last_reminder

    def __unicode__(self):
        # type: () -> text_type
        return u"<UserProfile: %s %s>" % (self.email, self.realm)

    @property
    def is_incoming_webhook(self):
        # type: () -> bool
        return self.bot_type == UserProfile.INCOMING_WEBHOOK_BOT

    @staticmethod
    def emails_from_ids(user_ids):
        # type: (Sequence[int]) -> Dict[int, text_type]
        rows = UserProfile.objects.filter(id__in=user_ids).values('id', 'email')
        return {row['id']: row['email'] for row in rows}

    def can_create_streams(self):
        # type: () -> bool
        if self.is_realm_admin or not self.realm.create_stream_by_admins_only:
            return True
        else:
            return False

    def major_tos_version(self):
        # type: () -> int
        if self.tos_version is not None:
            return int(self.tos_version.split('.')[0])
        else:
            return -1

def receives_offline_notifications(user_profile):
    # type: (UserProfile) -> bool
    return ((user_profile.enable_offline_email_notifications or
             user_profile.enable_offline_push_notifications) and
            not user_profile.is_bot)

# Make sure we flush the UserProfile object from our remote cache
# whenever we save it.
post_save.connect(flush_user_profile, sender=UserProfile)

class PreregistrationUser(models.Model):
    email = models.EmailField() # type: text_type
    referred_by = models.ForeignKey(UserProfile, null=True) # Optional[UserProfile]
    streams = models.ManyToManyField('Stream') # type: Manager
    invited_at = models.DateTimeField(auto_now=True) # type: datetime.datetime
    realm_creation = models.BooleanField(default=False)

    # status: whether an object has been confirmed.
    #   if confirmed, set to confirmation.settings.STATUS_ACTIVE
    status = models.IntegerField(default=0) # type: int

    realm = models.ForeignKey(Realm, null=True) # type: Optional[Realm]

class PushDeviceToken(models.Model):
    APNS = 1
    GCM = 2

    KINDS = (
        (APNS,  'apns'),
        (GCM,   'gcm'),
    )

    kind = models.PositiveSmallIntegerField(choices=KINDS) # type: int

    # The token is a unique device-specific token that is
    # sent to us from each device:
    #   - APNS token if kind == APNS
    #   - GCM registration id if kind == GCM
    token = models.CharField(max_length=4096, unique=True) # type: text_type
    last_updated = models.DateTimeField(auto_now=True) # type: datetime.datetime

    # The user who's device this is
    user = models.ForeignKey(UserProfile, db_index=True) # type: UserProfile

    # [optional] Contains the app id of the device if it is an iOS device
    ios_app_id = models.TextField(null=True) # type: Optional[text_type]

def generate_email_token_for_stream():
    # type: () -> text_type
    return generate_random_token(32)

class Stream(ModelReprMixin, models.Model):
    MAX_NAME_LENGTH = 60
    name = models.CharField(max_length=MAX_NAME_LENGTH, db_index=True) # type: text_type
    realm = models.ForeignKey(Realm, db_index=True) # type: Realm
    invite_only = models.NullBooleanField(default=False) # type: Optional[bool]
    # Used by the e-mail forwarder. The e-mail RFC specifies a maximum
    # e-mail length of 254, and our max stream length is 30, so we
    # have plenty of room for the token.
    email_token = models.CharField(
        max_length=32, default=generate_email_token_for_stream) # type: text_type
    description = models.CharField(max_length=1024, default=u'') # type: text_type

    date_created = models.DateTimeField(default=timezone.now) # type: datetime.datetime
    deactivated = models.BooleanField(default=False) # type: bool

    def __unicode__(self):
        # type: () -> text_type
        return u"<Stream: %s>" % (self.name,)

    def is_public(self):
        # type: () -> bool
        # All streams are private in Zephyr mirroring realms.
        return not self.invite_only and not self.realm.is_zephyr_mirror_realm

    class Meta(object):
        unique_together = ("name", "realm")

    def num_subscribers(self):
        # type: () -> int
        return Subscription.objects.filter(
                recipient__type=Recipient.STREAM,
                recipient__type_id=self.id,
                user_profile__is_active=True,
                active=True
        ).count()

    # This is stream information that is sent to clients
    def to_dict(self):
        # type: () -> Dict[str, Any]
        return dict(name=self.name,
                    stream_id=self.id,
                    description=self.description,
                    invite_only=self.invite_only)

post_save.connect(flush_stream, sender=Stream)
post_delete.connect(flush_stream, sender=Stream)

def valid_stream_name(name):
    # type: (text_type) -> bool
    return name != ""

# The Recipient table is used to map Messages to the set of users who
# received the message.  It is implemented as a set of triples (id,
# type_id, type). We have 3 types of recipients: Huddles (for group
# private messages), UserProfiles (for 1:1 private messages), and
# Streams. The recipient table maps a globally unique recipient id
# (used by the Message table) to the type-specific unique id (the
# stream id, user_profile id, or huddle id).
class Recipient(ModelReprMixin, models.Model):
    type_id = models.IntegerField(db_index=True) # type: int
    type = models.PositiveSmallIntegerField(db_index=True) # type: int
    # Valid types are {personal, stream, huddle}
    PERSONAL = 1
    STREAM = 2
    HUDDLE = 3

    class Meta(object):
        unique_together = ("type", "type_id")

    # N.B. If we used Django's choice=... we would get this for free (kinda)
    _type_names = {
        PERSONAL: 'personal',
        STREAM:   'stream',
        HUDDLE:   'huddle' }

    def type_name(self):
        # type: () -> str
        # Raises KeyError if invalid
        return self._type_names[self.type]

    def __unicode__(self):
        # type: () -> text_type
        display_recipient = get_display_recipient(self)
        return u"<Recipient: %s (%d, %s)>" % (display_recipient, self.type_id, self.type)

class Client(ModelReprMixin, models.Model):
    name = models.CharField(max_length=30, db_index=True, unique=True) # type: text_type

    def __unicode__(self):
        # type: () -> text_type
        return u"<Client: %s>" % (self.name,)

get_client_cache = {} # type: Dict[text_type, Client]
def get_client(name):
    # type: (text_type) -> Client
    if name not in get_client_cache:
        result = get_client_remote_cache(name)
        get_client_cache[name] = result
    return get_client_cache[name]

def get_client_cache_key(name):
    # type: (text_type) -> text_type
    return u'get_client:%s' % (make_safe_digest(name),)

@cache_with_key(get_client_cache_key, timeout=3600*24*7)
def get_client_remote_cache(name):
    # type: (text_type) -> Client
    (client, _) = Client.objects.get_or_create(name=name)
    return client

# get_stream_backend takes either a realm id or a realm
@cache_with_key(get_stream_cache_key, timeout=3600*24*7)
def get_stream_backend(stream_name, realm):
    # type: (text_type, Realm) -> Stream
    return Stream.objects.select_related("realm").get(
        name__iexact=stream_name.strip(), realm_id=realm.id)

def get_active_streams(realm):
    # type: (Realm) -> QuerySet
    """
    Return all streams (including invite-only streams) that have not been deactivated.
    """
    return Stream.objects.filter(realm=realm, deactivated=False)

def get_stream(stream_name, realm):
    # type: (text_type, Realm) -> Optional[Stream]
    try:
        return get_stream_backend(stream_name, realm)
    except Stream.DoesNotExist:
        return None

def bulk_get_streams(realm, stream_names):
    # type: (Realm, STREAM_NAMES) -> Dict[text_type, Any]

    def fetch_streams_by_name(stream_names):
        # type: (List[text_type]) -> Sequence[Stream]
        #
        # This should be just
        #
        # Stream.objects.select_related("realm").filter(name__iexact__in=stream_names,
        #                                               realm_id=realm_id)
        #
        # But chaining __in and __iexact doesn't work with Django's
        # ORM, so we have the following hack to construct the relevant where clause
        if len(stream_names) == 0:
            return []
        upper_list = ", ".join(["UPPER(%s)"] * len(stream_names))
        where_clause = "UPPER(zerver_stream.name::text) IN (%s)" % (upper_list,)
        return get_active_streams(realm.id).select_related("realm").extra(
            where=[where_clause],
            params=stream_names)

    return generic_bulk_cached_fetch(lambda stream_name: get_stream_cache_key(stream_name, realm),
                                     fetch_streams_by_name,
                                     [stream_name.lower() for stream_name in stream_names],
                                     id_fetcher=lambda stream: stream.name.lower())

def get_recipient_cache_key(type, type_id):
    # type: (int, int) -> text_type
    return u"get_recipient:%s:%s" % (type, type_id,)

@cache_with_key(get_recipient_cache_key, timeout=3600*24*7)
def get_recipient(type, type_id):
    # type: (int, int) -> Recipient
    return Recipient.objects.get(type_id=type_id, type=type)

def bulk_get_recipients(type, type_ids):
    # type: (int, List[int]) -> Dict[int, Any]
    def cache_key_function(type_id):
        # type: (int) -> text_type
        return get_recipient_cache_key(type, type_id)
    def query_function(type_ids):
        # type: (List[int]) -> Sequence[Recipient]
        # TODO: Change return type to QuerySet[Recipient]
        return Recipient.objects.filter(type=type, type_id__in=type_ids)

    return generic_bulk_cached_fetch(cache_key_function, query_function, type_ids,
                                     id_fetcher=lambda recipient: recipient.type_id)

def extract_message_dict(message_bytes):
    # type: (binary_type) -> Dict[str, Any]
    return dict_with_str_keys(ujson.loads(zlib.decompress(message_bytes).decode("utf-8")))

def stringify_message_dict(message_dict):
    # type: (Dict[str, Any]) -> binary_type
    return zlib.compress(force_bytes(ujson.dumps(message_dict)))

def to_dict_cache_key_id(message_id, apply_markdown):
    # type: (int, bool) -> text_type
    return u'message_dict:%d:%d' % (message_id, apply_markdown)

def to_dict_cache_key(message, apply_markdown):
    # type: (Message, bool) -> text_type
    return to_dict_cache_key_id(message.id, apply_markdown)

class Message(ModelReprMixin, models.Model):
    sender = models.ForeignKey(UserProfile) # type: UserProfile
    recipient = models.ForeignKey(Recipient) # type: Recipient
    subject = models.CharField(max_length=MAX_SUBJECT_LENGTH, db_index=True) # type: text_type
    content = models.TextField() # type: text_type
    rendered_content = models.TextField(null=True) # type: Optional[text_type]
    rendered_content_version = models.IntegerField(null=True) # type: Optional[int]
    pub_date = models.DateTimeField('date published', db_index=True) # type: datetime.datetime
    sending_client = models.ForeignKey(Client) # type: Client
    last_edit_time = models.DateTimeField(null=True) # type: Optional[datetime.datetime]
    edit_history = models.TextField(null=True) # type: Optional[text_type]
    has_attachment = models.BooleanField(default=False, db_index=True) # type: bool
    has_image = models.BooleanField(default=False, db_index=True) # type: bool
    has_link = models.BooleanField(default=False, db_index=True) # type: bool

    def topic_name(self):
        # type: () -> text_type
        """
        Please start using this helper to facilitate an
        eventual switch over to a separate topic table.
        """
        return self.subject

    def __unicode__(self):
        # type: () -> text_type
        display_recipient = get_display_recipient(self.recipient)
        return u"<Message: %s / %s / %r>" % (display_recipient, self.subject, self.sender)

    def get_realm(self):
        # type: () -> Realm
        return self.sender.realm

    def render_markdown(self, content, domain=None):
        # type: (text_type, Optional[text_type]) -> text_type
        """Return HTML for given markdown. Bugdown may add properties to the
        message object such as `mentions_user_ids` and `mentions_wildcard`.
        These are only on this Django object and are not saved in the
        database.
        """
        # TODO: see #1379 to eliminate bugdown dependencies
        global bugdown
        if bugdown is None:
            import zerver.lib.bugdown as bugdown
            # 'from zerver.lib import bugdown' gives mypy error in python 3 mode.

        self.mentions_wildcard = False
        self.is_me_message = False
        self.mentions_user_ids = set() # type: Set[int]
        self.user_ids_with_alert_words = set() # type: Set[int]

        if not domain:
            domain = self.sender.realm.domain
        if self.sending_client.name == "zephyr_mirror" and self.sender.realm.is_zephyr_mirror_realm:
            # Use slightly customized Markdown processor for content
            # delivered via zephyr_mirror
            domain = u"zephyr_mirror"
        rendered_content = bugdown.convert(content, domain, self)

        self.is_me_message = Message.is_status_message(content, rendered_content)

        return rendered_content

    def set_rendered_content(self, rendered_content, save = False):
        # type: (text_type, bool) -> bool
        """Set the content on the message.
        """
        # TODO: see #1379 to eliminate bugdown dependencies
        global bugdown
        if bugdown is None:
            import zerver.lib.bugdown as bugdown
            # 'from zerver.lib import bugdown' gives mypy error in python 3 mode.

        self.rendered_content = rendered_content
        self.rendered_content_version = bugdown.version

        if self.rendered_content is not None:
            if save:
                self.save_rendered_content()
            return True
        else:
            return False

    def save_rendered_content(self):
        # type: () -> None
        self.save(update_fields=["rendered_content", "rendered_content_version"])

    def maybe_render_content(self, domain, save = False):
        # type: (Optional[text_type], bool) -> bool
        """Render the markdown if there is no existing rendered_content"""
        # TODO: see #1379 to eliminate bugdown dependencies
        global bugdown
        if bugdown is None:
            import zerver.lib.bugdown as bugdown
            # 'from zerver.lib import bugdown' gives mypy error in python 3 mode.

        if Message.need_to_render_content(self.rendered_content, self.rendered_content_version):
            return self.set_rendered_content(self.render_markdown(self.content, domain), save)
        else:
            return True

    @staticmethod
    def need_to_render_content(rendered_content, rendered_content_version):
        # type: (Optional[text_type], int) -> bool
        return rendered_content is None or rendered_content_version < bugdown.version

    def to_dict(self, apply_markdown):
        # type: (bool) -> Dict[str, Any]
        return extract_message_dict(self.to_dict_json(apply_markdown))

    @cache_with_key(to_dict_cache_key, timeout=3600*24)
    def to_dict_json(self, apply_markdown):
        # type: (bool) -> binary_type
        return self.to_dict_uncached(apply_markdown)

    def to_dict_uncached(self, apply_markdown):
        # type: (bool) -> binary_type
        return stringify_message_dict(self.to_dict_uncached_helper(apply_markdown))

    def to_dict_uncached_helper(self, apply_markdown):
        # type: (bool) -> Dict[str, Any]
        return Message.build_message_dict(
                apply_markdown = apply_markdown,
                message = self,
                message_id = self.id,
                last_edit_time = self.last_edit_time,
                edit_history = self.edit_history,
                content = self.content,
                subject = self.subject,
                pub_date = self.pub_date,
                rendered_content = self.rendered_content,
                rendered_content_version = self.rendered_content_version,
                sender_id = self.sender.id,
                sender_email = self.sender.email,
                sender_realm_domain = self.sender.realm.domain,
                sender_full_name = self.sender.full_name,
                sender_short_name = self.sender.short_name,
                sender_avatar_source = self.sender.avatar_source,
                sender_is_mirror_dummy = self.sender.is_mirror_dummy,
                sending_client_name = self.sending_client.name,
                recipient_id = self.recipient.id,
                recipient_type = self.recipient.type,
                recipient_type_id = self.recipient.type_id,
        )

    @staticmethod
    def build_dict_from_raw_db_row(row, apply_markdown):
        # type: (Dict[str, Any], bool) -> Dict[str, Any]
        '''
        row is a row from a .values() call, and it needs to have
        all the relevant fields populated
        '''
        return Message.build_message_dict(
                apply_markdown = apply_markdown,
                message = None,
                message_id = row['id'],
                last_edit_time = row['last_edit_time'],
                edit_history = row['edit_history'],
                content = row['content'],
                subject = row['subject'],
                pub_date = row['pub_date'],
                rendered_content = row['rendered_content'],
                rendered_content_version = row['rendered_content_version'],
                sender_id = row['sender_id'],
                sender_email = row['sender__email'],
                sender_realm_domain = row['sender__realm__domain'],
                sender_full_name = row['sender__full_name'],
                sender_short_name = row['sender__short_name'],
                sender_avatar_source = row['sender__avatar_source'],
                sender_is_mirror_dummy = row['sender__is_mirror_dummy'],
                sending_client_name = row['sending_client__name'],
                recipient_id = row['recipient_id'],
                recipient_type = row['recipient__type'],
                recipient_type_id = row['recipient__type_id'],
        )

    @staticmethod
    def build_message_dict(
            apply_markdown,
            message,
            message_id,
            last_edit_time,
            edit_history,
            content,
            subject,
            pub_date,
            rendered_content,
            rendered_content_version,
            sender_id,
            sender_email,
            sender_realm_domain,
            sender_full_name,
            sender_short_name,
            sender_avatar_source,
            sender_is_mirror_dummy,
            sending_client_name,
            recipient_id,
            recipient_type,
            recipient_type_id,
    ):
        # type: (bool, Message, int, datetime.datetime, text_type, text_type, text_type, datetime.datetime, text_type, Optional[int], int, text_type, text_type, text_type, text_type, text_type, bool, text_type, int, int, int) -> Dict[str, Any]
        # TODO: see #1379 to eliminate bugdown dependencies
        global bugdown
        if bugdown is None:
            import zerver.lib.bugdown as bugdown
            # 'from zerver.lib import bugdown' gives mypy error in python 3 mode.

        avatar_url = get_avatar_url(sender_avatar_source, sender_email)

        display_recipient = get_display_recipient_by_id(
                recipient_id,
                recipient_type,
                recipient_type_id
        )

        if recipient_type == Recipient.STREAM:
            display_type = "stream"
        elif recipient_type in (Recipient.HUDDLE, Recipient.PERSONAL):
            assert not isinstance(display_recipient, text_type)
            display_type = "private"
            if len(display_recipient) == 1:
                # add the sender in if this isn't a message between
                # someone and his self, preserving ordering
                recip = {'email': sender_email,
                         'domain': sender_realm_domain,
                         'full_name': sender_full_name,
                         'short_name': sender_short_name,
                         'id': sender_id,
                         'is_mirror_dummy': sender_is_mirror_dummy}
                if recip['email'] < display_recipient[0]['email']:
                    display_recipient = [recip, display_recipient[0]]
                elif recip['email'] > display_recipient[0]['email']:
                    display_recipient = [display_recipient[0], recip]

        obj = dict(
            id                = message_id,
            sender_email      = sender_email,
            sender_full_name  = sender_full_name,
            sender_short_name = sender_short_name,
            sender_domain     = sender_realm_domain,
            sender_id         = sender_id,
            type              = display_type,
            display_recipient = display_recipient,
            recipient_id      = recipient_id,
            subject           = subject,
            timestamp         = datetime_to_timestamp(pub_date),
            gravatar_hash     = gravatar_hash(sender_email), # Deprecated June 2013
            avatar_url        = avatar_url,
            client            = sending_client_name)

        obj['subject_links'] = bugdown.subject_links(sender_realm_domain.lower(), subject)

        if last_edit_time != None:
            obj['last_edit_timestamp'] = datetime_to_timestamp(last_edit_time)
            obj['edit_history'] = ujson.loads(edit_history)

        if apply_markdown:
            if Message.need_to_render_content(rendered_content, rendered_content_version):
                if message is None:
                    # We really shouldn't be rendering objects in this method, but there is
                    # a scenario where we upgrade the version of bugdown and fail to run
                    # management commands to re-render historical messages, and then we
                    # need to have side effects.  This method is optimized to not need full
                    # blown ORM objects, but the bugdown renderer is unfortunately highly
                    # coupled to Message, and we also need to persist the new rendered content.
                    # If we don't have a message object passed in, we get one here.  The cost
                    # of going to the DB here should be overshadowed by the cost of rendering
                    # and updating the row.
                    # TODO: see #1379 to eliminate bugdown dependencies
                    message = Message.objects.select_related().get(id=message_id)

                # It's unfortunate that we need to have side effects on the message
                # in some cases.
                rendered_content = message.render_markdown(content, sender_realm_domain)
                message.set_rendered_content(rendered_content, True)

            if rendered_content is not None:
                obj['content'] = rendered_content
            else:
                obj['content'] = u'<p>[Zulip note: Sorry, we could not understand the formatting of your message]</p>'

            obj['content_type'] = 'text/html'
        else:
            obj['content'] = content
            obj['content_type'] = 'text/x-markdown'

        return obj

    def to_log_dict(self):
        # type: () -> Dict[str, Any]
        return dict(
            id                = self.id,
            sender_id         = self.sender.id,
            sender_email      = self.sender.email,
            sender_domain     = self.sender.realm.domain,
            sender_full_name  = self.sender.full_name,
            sender_short_name = self.sender.short_name,
            sending_client    = self.sending_client.name,
            type              = self.recipient.type_name(),
            recipient         = get_display_recipient(self.recipient),
            subject           = self.topic_name(),
            content           = self.content,
            timestamp         = datetime_to_timestamp(self.pub_date))

    @staticmethod
    def get_raw_db_rows(needed_ids):
        # type: (List[int]) -> List[Dict[str, Any]]
        # This is a special purpose function optimized for
        # callers like get_old_messages_backend().
        fields = [
            'id',
            'subject',
            'pub_date',
            'last_edit_time',
            'edit_history',
            'content',
            'rendered_content',
            'rendered_content_version',
            'recipient_id',
            'recipient__type',
            'recipient__type_id',
            'sender_id',
            'sending_client__name',
            'sender__email',
            'sender__full_name',
            'sender__short_name',
            'sender__realm__id',
            'sender__realm__domain',
            'sender__avatar_source',
            'sender__is_mirror_dummy',
        ]
        return Message.objects.filter(id__in=needed_ids).values(*fields)

    @classmethod
    def remove_unreachable(cls):
        # type: (Any) -> None
        """Remove all Messages that are not referred to by any UserMessage."""
        cls.objects.exclude(id__in = UserMessage.objects.values('message_id')).delete()

    def sent_by_human(self):
        # type: () -> bool
        sending_client = self.sending_client.name.lower()

        return (sending_client in ('zulipandroid', 'zulipios', 'zulipdesktop',
                                   'website', 'ios', 'android')) or \
                                   ('desktop app' in sending_client)

    @staticmethod
    def content_has_attachment(content):
        # type: (text_type) -> Match
        return re.search(r'[/\-]user[\-_]uploads[/\.-]', content)

    @staticmethod
    def content_has_image(content):
        # type: (text_type) -> bool
        return bool(re.search(r'[/\-]user[\-_]uploads[/\.-]\S+\.(bmp|gif|jpg|jpeg|png|webp)', content, re.IGNORECASE))

    @staticmethod
    def content_has_link(content):
        # type: (text_type) -> bool
        return 'http://' in content or 'https://' in content or '/user_uploads' in content

    @staticmethod
    def is_status_message(content, rendered_content):
        # type: (text_type, text_type) -> bool
        """
        Returns True if content and rendered_content are from 'me_message'
        """
        if content.startswith('/me ') and '\n' not in content:
            if rendered_content.startswith('<p>') and rendered_content.endswith('</p>'):
                return True
        return False

    def update_calculated_fields(self):
        # type: () -> None
        # TODO: rendered_content could also be considered a calculated field
        content = self.content
        self.has_attachment = bool(Message.content_has_attachment(content))
        self.has_image = bool(Message.content_has_image(content))
        self.has_link = bool(Message.content_has_link(content))

@receiver(pre_save, sender=Message)
def pre_save_message(sender, **kwargs):
    # type: (Any, **Any) -> None
    if kwargs['update_fields'] is None or "content" in kwargs['update_fields']:
        message = kwargs['instance']
        message.update_calculated_fields()

def get_context_for_message(message):
    # type: (Message) -> Sequence[Message]
    # TODO: Change return type to QuerySet[Message]
    return Message.objects.filter(
        recipient_id=message.recipient_id,
        subject=message.subject,
        id__lt=message.id,
        pub_date__gt=message.pub_date - timedelta(minutes=15),
    ).order_by('-id')[:10]

def flush_message(sender, **kwargs):
    # type: (Any, **Any) -> None
    message = kwargs['instance']
    cache_delete(to_dict_cache_key(message, False))
    cache_delete(to_dict_cache_key(message, True))

post_save.connect(flush_message, sender=Message)

# Whenever a message is sent, for each user current subscribed to the
# corresponding Recipient object, we add a row to the UserMessage
# table, which has has columns (id, user profile id, message id,
# flags) indicating which messages each user has received.  This table
# allows us to quickly query any user's last 1000 messages to generate
# the home view.
#
# Additionally, the flags field stores metadata like whether the user
# has read the message, starred the message, collapsed or was
# mentioned the message, etc.
#
# UserMessage is the largest table in a Zulip installation, even
# though each row is only 4 integers.
class UserMessage(ModelReprMixin, models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    message = models.ForeignKey(Message) # type: Message
    # We're not using the archived field for now, but create it anyway
    # since this table will be an unpleasant one to do schema changes
    # on later
    ALL_FLAGS = ['read', 'starred', 'collapsed', 'mentioned', 'wildcard_mentioned',
                 'summarize_in_home', 'summarize_in_stream', 'force_expand', 'force_collapse',
                 'has_alert_word', "historical", 'is_me_message']
    flags = BitField(flags=ALL_FLAGS, default=0) # type: BitHandler

    class Meta(object):
        unique_together = ("user_profile", "message")

    def __unicode__(self):
        # type: () -> text_type
        display_recipient = get_display_recipient(self.message.recipient)
        return u"<UserMessage: %s / %s (%s)>" % (display_recipient, self.user_profile.email, self.flags_list())

    def flags_list(self):
        # type: () -> List[str]
        return [flag for flag in self.flags.keys() if getattr(self.flags, flag).is_set]

def parse_usermessage_flags(val):
    # type: (int) -> List[str]
    flags = []
    mask = 1
    for flag in UserMessage.ALL_FLAGS:
        if val & mask:
            flags.append(flag)
        mask <<= 1
    return flags

class Attachment(ModelReprMixin, models.Model):
    MAX_FILENAME_LENGTH = 100
    file_name = models.CharField(max_length=MAX_FILENAME_LENGTH, db_index=True) # type: text_type
    # path_id is a storage location agnostic representation of the path of the file.
    # If the path of a file is http://localhost:9991/user_uploads/a/b/abc/temp_file.py
    # then its path_id will be a/b/abc/temp_file.py.
    path_id = models.TextField(db_index=True) # type: text_type
    owner = models.ForeignKey(UserProfile) # type: UserProfile
    realm = models.ForeignKey(Realm, blank=True, null=True) # type: Realm
    is_realm_public = models.BooleanField(default=False) # type: bool
    messages = models.ManyToManyField(Message) # type: Manager
    create_time = models.DateTimeField(default=timezone.now, db_index=True) # type: datetime.datetime

    def __unicode__(self):
        # type: () -> text_type
        return u"<Attachment: %s>" % (self.file_name,)

    def is_claimed(self):
        # type: () -> bool
        return self.messages.count() > 0

    def get_url(self):
        # type: () -> text_type
        return u"/user_uploads/%s" % (self.path_id)

def get_attachments_by_owner_id(uid):
    # type: (int) -> Sequence[Attachment]
    # TODO: Change return type to QuerySet[Attachment]
    return Attachment.objects.filter(owner=uid).select_related('owner')

def get_owners_from_file_name(file_name):
    # type: (str) -> Sequence[Attachment]
    # TODO: Change return type to QuerySet[Attachment]
    # The returned vaule will list of owners since different users can upload
    # same files with the same filename.
    return Attachment.objects.filter(file_name=file_name).select_related('owner')

def get_old_unclaimed_attachments(weeks_ago):
    # type: (int) -> Sequence[Attachment]
    # TODO: Change return type to QuerySet[Attachment]
    delta_weeks_ago = timezone.now() - datetime.timedelta(weeks=weeks_ago)
    old_attachments = Attachment.objects.filter(messages=None, create_time__lt=delta_weeks_ago)
    return old_attachments

class Subscription(ModelReprMixin, models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    recipient = models.ForeignKey(Recipient) # type: Recipient
    active = models.BooleanField(default=True) # type: bool
    in_home_view = models.NullBooleanField(default=True) # type: Optional[bool]

    DEFAULT_STREAM_COLOR = u"#c2c2c2"
    color = models.CharField(max_length=10, default=DEFAULT_STREAM_COLOR) # type: text_type
    pin_to_top = models.BooleanField(default=False) # type: bool

    desktop_notifications = models.BooleanField(default=True) # type: bool
    audible_notifications = models.BooleanField(default=True) # type: bool

    # Combination desktop + audible notifications superseded by the
    # above.
    notifications = models.BooleanField(default=False) # type: bool

    class Meta(object):
        unique_together = ("user_profile", "recipient")

    def __unicode__(self):
        # type: () -> text_type
        return u"<Subscription: %r -> %s>" % (self.user_profile, self.recipient)

@cache_with_key(user_profile_by_id_cache_key, timeout=3600*24*7)
def get_user_profile_by_id(uid):
    # type: (int) -> UserProfile
    return UserProfile.objects.select_related().get(id=uid)

@cache_with_key(user_profile_by_email_cache_key, timeout=3600*24*7)
def get_user_profile_by_email(email):
    # type: (text_type) -> UserProfile
    return UserProfile.objects.select_related().get(email__iexact=email.strip())

@cache_with_key(active_user_dicts_in_realm_cache_key, timeout=3600*24*7)
def get_active_user_dicts_in_realm(realm):
    # type: (Realm) -> List[Dict[str, Any]]
    return UserProfile.objects.filter(realm=realm, is_active=True) \
                              .values(*active_user_dict_fields)

@cache_with_key(active_bot_dicts_in_realm_cache_key, timeout=3600*24*7)
def get_active_bot_dicts_in_realm(realm):
    # type: (Realm) -> List[Dict[str, Any]]
    return UserProfile.objects.filter(realm=realm, is_active=True, is_bot=True) \
                              .values(*active_bot_dict_fields)

def get_owned_bot_dicts(user_profile, include_all_realm_bots_if_admin=True):
    # type: (UserProfile, bool) -> List[Dict[str, Any]]
    if user_profile.is_realm_admin and include_all_realm_bots_if_admin:
        result = get_active_bot_dicts_in_realm(user_profile.realm)
    else:
        result = UserProfile.objects.filter(realm=user_profile.realm, is_active=True, is_bot=True,
                                        bot_owner=user_profile).values(*active_bot_dict_fields)
    return [{'email': botdict['email'],
             'full_name': botdict['full_name'],
             'api_key': botdict['api_key'],
             'default_sending_stream': botdict['default_sending_stream__name'],
             'default_events_register_stream': botdict['default_events_register_stream__name'],
             'default_all_public_streams': botdict['default_all_public_streams'],
             'owner': botdict['bot_owner__email'],
             'avatar_url': get_avatar_url(botdict['avatar_source'], botdict['email']),
            }
            for botdict in result]

def get_prereg_user_by_email(email):
    # type: (text_type) -> PreregistrationUser
    # A user can be invited many times, so only return the result of the latest
    # invite.
    return PreregistrationUser.objects.filter(email__iexact=email.strip()).latest("invited_at")

def get_cross_realm_users():
    # type: () -> Set[text_type]
    return set(settings.CROSS_REALM_BOT_EMAILS)

# The Huddle class represents a group of individuals who have had a
# Group Private Message conversation together.  The actual membership
# of the Huddle is stored in the Subscription table just like with
# Streams, and a hash of that list is stored in the huddle_hash field
# below, to support efficiently mapping from a set of users to the
# corresponding Huddle object.
class Huddle(models.Model):
    # TODO: We should consider whether using
    # CommaSeparatedIntegerField would be better.
    huddle_hash = models.CharField(max_length=40, db_index=True, unique=True) # type: text_type

def get_huddle_hash(id_list):
    # type: (List[int]) -> text_type
    id_list = sorted(set(id_list))
    hash_key = ",".join(str(x) for x in id_list)
    return make_safe_digest(hash_key)

def huddle_hash_cache_key(huddle_hash):
    # type: (text_type) -> text_type
    return u"huddle_by_hash:%s" % (huddle_hash,)

def get_huddle(id_list):
    # type: (List[int]) -> Huddle
    huddle_hash = get_huddle_hash(id_list)
    return get_huddle_backend(huddle_hash, id_list)

@cache_with_key(lambda huddle_hash, id_list: huddle_hash_cache_key(huddle_hash), timeout=3600*24*7)
def get_huddle_backend(huddle_hash, id_list):
    # type: (text_type, List[int]) -> Huddle
    (huddle, created) = Huddle.objects.get_or_create(huddle_hash=huddle_hash)
    if created:
        with transaction.atomic():
            recipient = Recipient.objects.create(type_id=huddle.id,
                                                 type=Recipient.HUDDLE)
            subs_to_create = [Subscription(recipient=recipient,
                                           user_profile=get_user_profile_by_id(user_profile_id))
                              for user_profile_id in id_list]
            Subscription.objects.bulk_create(subs_to_create)
    return huddle

def get_realm(domain):
    # type: (text_type) -> Optional[Realm]
    if not domain:
        return None
    try:
        return Realm.objects.get(domain__iexact=domain.strip())
    except Realm.DoesNotExist:
        return None

def clear_database():
    # type: () -> None
    pylibmc.Client(['127.0.0.1']).flush_all()
    model = None # type: Any
    for model in [Message, Stream, UserProfile, Recipient,
                  Realm, Subscription, Huddle, UserMessage, Client,
                  DefaultStream]:
        model.objects.all().delete()
    Session.objects.all().delete()

class UserActivity(models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    client = models.ForeignKey(Client) # type: Client
    query = models.CharField(max_length=50, db_index=True) # type: text_type

    count = models.IntegerField() # type: int
    last_visit = models.DateTimeField('last visit') # type: datetime.datetime

    class Meta(object):
        unique_together = ("user_profile", "client", "query")

class UserActivityInterval(models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    start = models.DateTimeField('start time', db_index=True) # type: datetime.datetime
    end = models.DateTimeField('end time', db_index=True) # type: datetime.datetime

class UserPresence(models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    client = models.ForeignKey(Client) # type: Client

    # Valid statuses
    ACTIVE = 1
    IDLE = 2

    timestamp = models.DateTimeField('presence changed') # type: datetime.datetime
    status = models.PositiveSmallIntegerField(default=ACTIVE) # type: int

    @staticmethod
    def status_to_string(status):
        # type: (int) -> str
        if status == UserPresence.ACTIVE:
            return 'active'
        elif status == UserPresence.IDLE:
            return 'idle'

    @staticmethod
    def get_status_dict_by_realm(realm_id):
        # type: (int) -> defaultdict[Any, Dict[Any, Any]]
        user_statuses = defaultdict(dict) # type: defaultdict[Any, Dict[Any, Any]]

        query = UserPresence.objects.filter(
                user_profile__realm_id=realm_id,
                user_profile__is_active=True,
                user_profile__is_bot=False
        ).values(
                'client__name',
                'status',
                'timestamp',
                'user_profile__email',
                'user_profile__id',
                'user_profile__enable_offline_push_notifications',
                'user_profile__is_mirror_dummy',
        )

        mobile_user_ids = [row['user'] for row in PushDeviceToken.objects.filter(
                user__realm_id=1,
                user__is_active=True,
                user__is_bot=False,
        ).distinct("user").values("user")]


        for row in query:
            info = UserPresence.to_presence_dict(
                    client_name=row['client__name'],
                    status=row['status'],
                    dt=row['timestamp'],
                    push_enabled=row['user_profile__enable_offline_push_notifications'],
                    has_push_devices=row['user_profile__id'] in mobile_user_ids,
                    is_mirror_dummy=row['user_profile__is_mirror_dummy'],
                    )
            user_statuses[row['user_profile__email']][row['client__name']] = info

        return user_statuses

    @staticmethod
    def to_presence_dict(client_name=None, status=None, dt=None, push_enabled=None,
                         has_push_devices=None, is_mirror_dummy=None):
        # type: (Optional[text_type], Optional[int], Optional[datetime.datetime], Optional[bool], Optional[bool], Optional[bool]) -> Dict[str, Any]
        presence_val = UserPresence.status_to_string(status)

        timestamp = datetime_to_timestamp(dt)
        return dict(
                client=client_name,
                status=presence_val,
                timestamp=timestamp,
                pushable=(push_enabled and has_push_devices),
        )

    def to_dict(self):
        # type: () -> Dict[str, Any]
        return UserPresence.to_presence_dict(
                client_name=self.client.name,
                status=self.status,
                dt=self.timestamp
        )

    @staticmethod
    def status_from_string(status):
        # type: (NonBinaryStr) -> Optional[int]
        if status == 'active':
            status_val = UserPresence.ACTIVE
        elif status == 'idle':
            status_val = UserPresence.IDLE
        else:
            status_val = None

        return status_val

    class Meta(object):
        unique_together = ("user_profile", "client")

class DefaultStream(models.Model):
    realm = models.ForeignKey(Realm) # type: Realm
    stream = models.ForeignKey(Stream) # type: Stream

    class Meta(object):
        unique_together = ("realm", "stream")

class Referral(models.Model):
    user_profile = models.ForeignKey(UserProfile) # type: UserProfile
    email = models.EmailField(blank=False, null=False) # type: text_type
    timestamp = models.DateTimeField(auto_now_add=True, null=False) # type: datetime.datetime

# This table only gets used on Zulip Voyager instances
# For reasons of deliverability (and sending from multiple email addresses),
# we will still send from mandrill when we send things from the (staging.)zulip.com install
class ScheduledJob(models.Model):
    scheduled_timestamp = models.DateTimeField(auto_now_add=False, null=False) # type: datetime.datetime
    type = models.PositiveSmallIntegerField() # type: int
    # Valid types are {email}
    # for EMAIL, filter_string is recipient_email
    EMAIL = 1

    # JSON representation of the job's data. Be careful, as we are not relying on Django to do validation
    data = models.TextField() # type: text_type
    # Kind if like a ForeignKey, but table is determined by type.
    filter_id = models.IntegerField(null=True) # type: Optional[int]
    filter_string = models.CharField(max_length=100) # type: text_type
