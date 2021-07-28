""" access the activity streams stored in redis """
from django.dispatch import receiver
from django.db import transaction
from django.db.models import signals, Q

from bookwyrm import models
from bookwyrm.redis_store import RedisStore, r
from bookwyrm.tasks import app
from bookwyrm.views.helpers import privacy_filter


class ActivityStream(RedisStore):
    """a category of activity stream (like home, local, federated)"""

    def stream_id(self, user_id):
        """the redis key for this user's instance of this stream"""
        return "{}-{}".format(user_id, self.key)

    def unread_id(self, user):
        """the redis key for this user's unread count for this stream"""
        return "{}-unread".format(self.stream_id(user.id))

    def get_rank(self, obj_id):  # pylint: disable=no-self-use
        """statuses are sorted by date published"""
        obj = models.Status.objects.get(id=obj_id)
        return obj.published_date.timestamp()

    def add_status(self, status_id):
        """add a status to users' feeds"""
        # the pipeline contains all the add-to-stream activities
        pipeline = self.add_object_to_related_stores(status_id, execute=False)

        for user in self.get_audience(status_id):
            # add to the unread status count
            pipeline.incr(self.unread_id(user))

        # and go!
        pipeline.execute()

    def add_user_statuses(self, viewer_id, user_id):
        """add a user's statuses to another user's feed"""
        # only add the statuses that the viewer should be able to see (ie, not dms)
        viewer = models.User.objects.get(id=viewer_id)
        statuses = privacy_filter(
            viewer, models.Status.objects.filter(user__id=user_id)
        ).limit(self.max_length)
        self.bulk_add_objects_to_store(statuses, self.stream_id(viewer_id))

    def remove_user_statuses(self, viewer_id, user_id):
        """remove a user's status from another user's feed"""
        # remove all so that followers only statuses are removed
        statuses = models.Status.objects.filter(user__id=user_id)
        self.bulk_remove_objects_from_store(statuses, self.stream_id(viewer_id))

    def get_activity_stream(self, user):
        """load the statuses to be displayed"""
        # clear unreads for this feed
        r.set(self.unread_id(user), 0)

        statuses = self.get_store(self.stream_id(user.id))
        return (
            models.Status.objects.select_subclasses()
            .filter(id__in=statuses)
            .select_related("user", "reply_parent")
            .prefetch_related("mention_books", "mention_users")
            .order_by("-published_date")
        )

    def get_unread_count(self, user):
        """get the unread status count for this user's feed"""
        return int(r.get(self.unread_id(user)) or 0)

    def populate_streams(self, user):
        """go from zero to a timeline"""
        self.populate_store(self.stream_id(user.id))

    def get_audience(self, status_id):  # pylint: disable=no-self-use
        """given a status, what users should see it"""
        status = models.Status.objects.select_subclasses().get(id=status_id)
        # direct messages don't appeard in feeds, direct comments/reviews/etc do
        if status.privacy == "direct" and status.status_type == "Note":
            return []

        # everybody who could plausibly see this status
        audience = models.User.objects.filter(
            is_active=True,
            local=True,  # we only create feeds for users of this instance
        ).exclude(
            Q(id__in=status.user.blocks.all()) | Q(blocks=status.user)  # not blocked
        )

        # only visible to the poster and mentioned users
        if status.privacy == "direct":
            audience = audience.filter(
                Q(id=status.user.id)  # if the user is the post's author
                | Q(id__in=status.mention_users.all())  # if the user is mentioned
            )
        # only visible to the poster's followers and tagged users
        elif status.privacy == "followers":
            audience = audience.filter(
                Q(id=status.user.id)  # if the user is the post's author
                | Q(following=status.user)  # if the user is following the author
            )
        return audience.distinct()

    def get_stores_for_object(self, obj_id):
        return [self.stream_id(u.id) for u in self.get_audience(obj_id)]

    def get_statuses_for_user(self, user):  # pylint: disable=no-self-use
        """given a user, what statuses should they see on this stream"""
        return privacy_filter(
            user,
            models.Status.objects.select_subclasses(),
            privacy_levels=["public", "unlisted", "followers"],
        )

    def get_objects_for_store(self, store):
        user = models.User.objects.get(id=store.split("-")[0])
        return self.get_statuses_for_user(user)


class HomeStream(ActivityStream):
    """users you follow"""

    key = "home"

    def get_audience(self, status_id):
        audience = super().get_audience(status_id)
        if not audience:
            return []
        status = models.Status.objects.select_subclasses().get(id=status_id)
        return audience.filter(
            Q(id=status.user.id)  # if the user is the post's author
            | Q(following=status.user)  # if the user is following the author
        ).distinct()

    def get_statuses_for_user(self, user):
        return privacy_filter(
            user,
            models.Status.objects.select_subclasses(),
            privacy_levels=["public", "unlisted", "followers"],
            following_only=True,
        )


class LocalStream(ActivityStream):
    """users you follow"""

    key = "local"

    def get_audience(self, status_id):
        # this stream wants no part in non-public statuses
        status = models.Status.objects.select_subclasses().get(id=status_id)
        if status.privacy != "public" or not status.user.local:
            return []
        return super().get_audience(status_id)

    def get_statuses_for_user(self, user):
        # all public statuses by a local user
        return privacy_filter(
            user,
            models.Status.objects.select_subclasses().filter(user__local=True),
            privacy_levels=["public"],
        )


class FederatedStream(ActivityStream):
    """users you follow"""

    key = "federated"

    def get_audience(self, status_id):
        # this stream wants no part in non-public statuses
        status = models.Status.objects.select_subclasses().get(id=status_id)
        if status.privacy != "public":
            return []
        return super().get_audience(status_id)

    def get_statuses_for_user(self, user):
        return privacy_filter(
            user,
            models.Status.objects.select_subclasses(),
            privacy_levels=["public"],
        )


streams = {
    "home": HomeStream(),
    "local": LocalStream(),
    "federated": FederatedStream(),
}


@receiver(signals.post_save)
# pylint: disable=unused-argument
def add_status_on_create(sender, instance, created, *args, **kwargs):
    """add newly created statuses to activity feeds"""
    # we're only interested in new statuses
    if not issubclass(sender, models.Status):
        return

    if instance.deleted:
        remove_status_task.delay(instance.id)
        return

    if not created:
        return

    # when creating new things, gotta wait on the transaction
    transaction.on_commit(lambda: add_status_on_create_command(sender, instance))


def add_status_on_create_command(sender, instance):
    """runs this code only after the database commit completes"""
    # iterates through Home, Local, Federated
    add_status_task.delay(instance.id)

    if sender != models.Boost:
        return

    # remove the original post and other, earlier boosts
    boosted = instance.boost.boosted_status
    old_versions = models.Boost.objects.filter(
        boosted_status__id=boosted.id,
        created_date__lt=instance.created_date,
    )
    remove_status_task.delay(boosted.id)
    remove_status_task.delay([o.id for o in old_versions])


@receiver(signals.post_delete, sender=models.Boost)
# pylint: disable=unused-argument
def remove_boost_on_delete(sender, instance, *args, **kwargs):
    """boosts are deleted"""
    # remove the boost
    remove_status_task.delay(instance.id)
    # re-add the original status
    add_status_task.delay(instance.boosted_status.id)


@receiver(signals.post_save, sender=models.UserFollows)
# pylint: disable=unused-argument
def add_statuses_on_follow(sender, instance, created, *args, **kwargs):
    """add a newly followed user's statuses to feeds"""
    if not created or not instance.user_subject.local:
        return
    add_user_statuses_task.delay(
        instance.user_subject.id, instance.user_object.id, ["home"]
    )


@receiver(signals.post_delete, sender=models.UserFollows)
# pylint: disable=unused-argument
def remove_statuses_on_unfollow(sender, instance, *args, **kwargs):
    """remove statuses from a feed on unfollow"""
    if not instance.user_subject.local:
        return
    remove_user_statuses_task.delay(
        instance.user_subject.id, instance.user_object.id, stream_list=["home"]
    )


@receiver(signals.post_save, sender=models.UserBlocks)
# pylint: disable=unused-argument
def remove_statuses_on_block(sender, instance, *args, **kwargs):
    """remove statuses from all feeds on block"""
    # blocks apply ot all feeds
    if instance.user_subject.local:
        remove_user_statuses_task.delay(
            instance.user_subject.id, instance.user_object.id
        )

    # and in both directions
    if instance.user_object.local:
        remove_user_statuses_task.delay(
            instance.user_object.id, instance.user_subject.id
        )


@receiver(signals.post_delete, sender=models.UserBlocks)
# pylint: disable=unused-argument
def add_statuses_on_unblock(sender, instance, *args, **kwargs):
    """remove statuses from all feeds on block"""
    # add statuses back to streams with statuses from anyone
    if instance.user_subject.local:
        add_user_statuses_task.delay(
            instance.user_subject.id, instance.user_object.id, ["local", "federated"]
        )

    # and the same for the unblocked user
    if instance.user_object.local:
        add_user_statuses_task.delay(
            instance.user_object.id, instance.user_subject.id, ["local", "federated"]
        )


@receiver(signals.post_save, sender=models.User)
# pylint: disable=unused-argument
def populate_streams_on_account_create(sender, instance, created, *args, **kwargs):
    """build a user's feeds when they join"""
    if not created or not instance.local:
        return

    for stream in streams.values():
        stream.populate_streams(instance)


@app.task
def remove_status_task(status_ids):
    """remove a status from any stream it might be in"""
    # this can take an id or a list of ids
    if not isinstance(status_ids, list):
        status_ids = [status_ids]

    for stream in streams.values():
        for status_id in status_ids:
            stream.remove_object_from_related_stores(status_id)


@app.task
def add_status_task(status_id):
    """remove a status from any stream it might be in"""
    # this can take an id or a list of ids
    for stream in streams.values():
        stream.add_status(status_id)


@app.task
def remove_user_statuses_task(viewer_id, user_id, stream_list=None):
    """remove all statuses by a user from a viewer's stream"""
    stream_list = [streams[s] for s in stream_list] or streams
    for stream in stream_list:
        stream.remove_user_statuses(viewer_id, user_id)


@app.task
def add_user_statuses_task(viewer_id, user_id, stream_list=None):
    """remove all statuses by a user from a viewer's stream"""
    stream_list = [streams[s] for s in stream_list] or streams
    for stream in stream_list:
        stream.add_user_statuses(viewer_id, user_id)
