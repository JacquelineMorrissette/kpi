# coding: utf-8
import copy
import json
from collections import defaultdict
from typing import Union

from django.contrib.auth.models import Permission, User
from django.urls import Resolver404
from django.utils.translation import gettext as t
from rest_framework import serializers
from rest_framework.reverse import reverse

from kpi.constants import (
    PERM_PARTIAL_SUBMISSIONS,
    PREFIX_PARTIAL_PERMS,
    SUFFIX_SUBMISSIONS_PERMS,
)
from kpi.fields.relative_prefix_hyperlinked_related import (
    RelativePrefixHyperlinkedRelatedField,
)
from kpi.models.asset import Asset, AssetUserPartialPermission
from kpi.models.object_permission import ObjectPermission
from kpi.utils.object_permission import (
    get_user_permission_assignments_queryset,
)
from kpi.utils.urls import absolute_resolve


ASSIGN_OWNER_ERROR_MESSAGE = "Owner's permissions cannot be assigned explicitly"


class AssetPermissionAssignmentSerializer(serializers.ModelSerializer):

    url = serializers.SerializerMethodField()
    user = RelativePrefixHyperlinkedRelatedField(
        view_name='user-detail',
        lookup_field='username',
        queryset=User.objects.all(),
        style={'base_template': 'input.html'}  # Render as a simple text box
    )
    permission = RelativePrefixHyperlinkedRelatedField(
        view_name='permission-detail',
        lookup_field='codename',
        queryset=Permission.objects.all(),
        style={'base_template': 'input.html'}  # Render as a simple text box
    )
    partial_permissions = serializers.SerializerMethodField()
    label = serializers.SerializerMethodField()

    class Meta:
        model = ObjectPermission
        fields = (
            'url',
            'user',
            'permission',
            'partial_permissions',
            'label',
        )

        read_only_fields = ('uid', 'label')

    def create(self, validated_data):
        user = validated_data['user']
        asset = validated_data['asset']
        if asset.owner_id == user.id:
            raise serializers.ValidationError(
                {'user': t(ASSIGN_OWNER_ERROR_MESSAGE)}
            )
        permission = validated_data['permission']
        partial_permissions = validated_data.get('partial_permissions', None)

        return asset.assign_perm(
            user, permission.codename, partial_perms=partial_permissions
        )

    def get_label(self, object_permission):
        # `self.object_permission.label` calls `self.object_permission.asset`
        # internally. Thus, costs an extra query each time the object is
        # serialized. `asset` is already loaded and attached to context,
        # let's use it!
        try:
            asset = self.context['asset']
        except KeyError:
            return object_permission.label
        else:
            return asset.get_label_for_permission(
                object_permission.permission.codename
            )

    def get_partial_permissions(self, object_permission):
        codename = object_permission.permission.codename
        if codename.startswith(PREFIX_PARTIAL_PERMS):
            view = self.context.get('view')
            # if view doesn't have an `asset` property,
            # fallback to context. (e.g. AssetViewSet)
            asset = getattr(view, 'asset', self.context.get('asset'))
            partial_perms = asset.get_partial_perms(
                object_permission.user_id, with_filters=True)
            if not partial_perms:
                return None

            if partial_perms:
                hyperlinked_partial_perms = []
                for perm_codename, filters in partial_perms.items():
                    url = self.__get_permission_hyperlink(perm_codename)
                    hyperlinked_partial_perms.append({
                        'url': url,
                        'filters': filters
                    })
                return hyperlinked_partial_perms
        return None

    def get_url(self, object_permission):
        asset_uid = self.context.get('asset_uid')
        return reverse('asset-permission-assignment-detail',
                       args=(asset_uid, object_permission.uid),
                       request=self.context.get('request', None))

    def validate(self, attrs):
        # Because `partial_permissions` is a `SerializerMethodField`,
        # it's read-only, so it's not validated nor added to `validated_data`.
        # We need to do it manually
        self.validate_partial_permissions(attrs)
        return attrs

    def validate_partial_permissions(self, attrs):
        """
        Validates permissions and filters sent with partial permissions.

        If data is valid, `partial_permissions` attribute is added to `attrs`.
        Useful to permission assignment in `create()`.

        :param attrs: dict of `{'user': '<username>',
                                'permission': <permission object>}`
        :return: dict, the `attrs` parameter updated (if necessary) with
                 validated `partial_permissions` dicts.
        """
        permission = attrs['permission']
        if not permission.codename.startswith(PREFIX_PARTIAL_PERMS):
            # No additional validation needed
            return attrs

        def _invalid_partial_permissions(message):
            raise serializers.ValidationError(
                {'partial_permissions': message}
            )

        request = self.context['request']
        partial_permissions = None

        if isinstance(request.data, dict):  # for a single assignment
            partial_permissions = request.data.get('partial_permissions')
        elif self.context.get('partial_permissions'):  # injected during bulk assignment
            partial_permissions = self.context.get('partial_permissions')

        if not partial_permissions:
            _invalid_partial_permissions(
                t("This field is required for the '{}' permission").format(
                    permission.codename
                )
            )

        partial_permissions_attr = defaultdict(list)

        for partial_permission, filters_ in \
                self.__get_partial_permissions_generator(partial_permissions):
            try:
                resolver_match = absolute_resolve(
                    partial_permission.get('url')
                )
            except (TypeError, Resolver404):
                _invalid_partial_permissions(t('Invalid `url`'))

            try:
                codename = resolver_match.kwargs['codename']
            except KeyError:
                _invalid_partial_permissions(t('Invalid `url`'))

            # Permission must valid and must be assignable.
            if not self._validate_permission(codename,
                                             SUFFIX_SUBMISSIONS_PERMS):
                _invalid_partial_permissions(t('Invalid `url`'))

            # No need to validate Mongo syntax, query will fail
            # if syntax is not correct.
            if not isinstance(filters_, dict):
                _invalid_partial_permissions(t('Invalid `filters`'))

            # Validation passed!
            partial_permissions_attr[codename].append(filters_)

        # Everything went well. Add it to `attrs`
        attrs.update({'partial_permissions': partial_permissions_attr})

        return attrs

    def validate_permission(self, permission):
        """
        Checks if permission can be assigned on asset.
        """
        if not self._validate_permission(permission.codename):
            raise serializers.ValidationError(
                t(
                    '{permission} cannot be assigned explicitly to '
                    'Asset objects of this type.'
                ).format(permission=permission.codename)
            )
        return permission

    def to_representation(self, instance):
        """
        Doesn't display 'partial_permissions' attribute if it's `None`.
        """
        try:
            # Each time we try to access `instance.label`, `instance.content_object`
            # is needed. Django can't find it from objects cache even if it already
            # exists. Because of `GenericForeignKey`, `select_related` can't be
            # used to load it within the same queryset. So Django hits DB each
            # time a label is shown. `prefetch_related` helps, but not that much.
            # It still needs to load object from DB at least once.
            # It means, when listing assets, it would add as many extra queries
            # as assets. `content_object`, in that case, is the parent asset and
            # we can access it through the context. Let's use it.
            asset = self.context['asset']
            setattr(instance, 'content_object', asset)
        except KeyError:
            pass

        repr_ = super().to_representation(instance)
        repr_copy = dict(repr_)
        for k, v in repr_copy.items():
            if k == 'partial_permissions' and v is None:
                del repr_[k]

        return repr_

    def _validate_permission(self, codename, suffix=None):
        """
        Validates if `codename` can be assigned on `Asset`s.
        Search can be restricted to assignable codenames which end with `suffix`

        :param codename: str. See `Asset.ASSIGNABLE_PERMISSIONS
        :param suffix: str.
        :return: bool.
        """
        return (
            # DONOTMERGE abusive to the database server?
            codename in Asset.objects.only('asset_type').get(
                uid=self.context['asset_uid']
            ).get_assignable_permissions(
                with_partial=True
            ) and (suffix is None or codename.endswith(suffix))
        )

    def __get_partial_permissions_generator(self, partial_permissions):
        """
        Creates a generator to iterate over partial_permissions list.
        Useful to validate each item and stop iterating as soon as errors
        are detected

        :param partial_permissions: list
        :return: generator
        """
        for partial_permission in partial_permissions:
            for filters_ in partial_permission.get('filters'):
                yield partial_permission, filters_

    def __get_permission_hyperlink(self, codename):
        """
        Builds permission hyperlink representation.
        :param codename: str
        :return: str. url
        """
        return reverse('permission-detail',
                       args=(codename,),
                       request=self.context.get('request', None))


class PartialPermissionField(serializers.Field):

    default_error_messages = {
        'invalid': t('Not a valid list.'),
        'blank': t('This field may not be blank.'),
    }
    initial = ''

    def __init__(self, **kwargs):
        super().__init__(required=False, **kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, list):
            self.fail('invalid')
        return data

    def to_representation(self, value):
        return value


class PermissionAssignmentSerializer(serializers.Serializer):

    user = serializers.CharField()
    permission = serializers.CharField()
    partial_permissions = PartialPermissionField()


class AssetBulkInsertPermissionSerializer(serializers.Serializer):
    """
    This goal of this class is not to expose data in API endpoint,
    but to process all validation checks and convert URLs into objects at once
    to avoid multiple queries to DB vs AssetPermissionAssignmentSerializer(many=True)
    which makes a query to DB to match each RelativePrefixHyperlinkedRelatedField()
    with an Django model object.

    Warning: If less queries are sent to DB, it consumes more CPU and memory.
    The bigger the assignments are, the bigger the resources footprint will be.
    """
    assignments = serializers.ListField(child=PermissionAssignmentSerializer())

    def create(self, validated_data):
        asset = self.context['asset']
        # TODO: refactor permission assignment code so that it does not always
        # require a fully-fledged `User` object?
        user_pk_to_obj = dict()

        # Build a set of all incoming assignments, including implied
        # assignments not explicitly sent by the front end
        incoming_assignments = set()
        for incoming_assignment in validated_data['assignments']:
            incoming_permission = incoming_assignment['permission']
            partial_permissions = None

            # Stupid object cache thing because `assign_perm()` and
            # `remove_perm()` REQUIRE user objects
            user_pk_to_obj[
                incoming_assignment['user'].pk
            ] = incoming_assignment['user']

            if incoming_permission.codename == PERM_PARTIAL_SUBMISSIONS:
                # Expand to include implied partial permissions
                for partially_granted_codename, filter_criteria in (
                    incoming_assignment['partial_permissions'].copy().items()
                ):
                    for implied_codename in asset.get_implied_perms(
                        partially_granted_codename, for_instance=asset
                    ):
                        if not implied_codename.endswith(
                            SUFFIX_SUBMISSIONS_PERMS
                        ):
                            continue
                        incoming_assignment['partial_permissions'][
                            implied_codename
                        ] = filter_criteria
                partial_permissions = json.dumps(
                    incoming_assignment['partial_permissions'], sort_keys=True
                )
            else:
                # Expand to include regular implied permissions
                for implied_codename in asset.get_implied_perms(
                    incoming_permission.codename, for_instance=asset
                ):
                    incoming_assignments.add(
                        (incoming_assignment['user'].pk, implied_codename, None)
                    )

            incoming_assignments.add(
                (
                    incoming_assignment['user'].pk,
                    incoming_permission.codename,
                    partial_permissions,
                )
            )

        # Get all existing partial permissions with a single query and store
        # in a dictionary where the keys are the primary key of each user
        existing_partial_perms_for_user = dict(
            asset.asset_partial_permissions.values_list('user', 'permissions')
        )

        # Build a set of all existing assignments
        existing_assignments = set(
            (
                user,
                codename,
                json.dumps(
                    existing_partial_perms_for_user[user], sort_keys=True
                )
                if codename == PERM_PARTIAL_SUBMISSIONS
                else None,
            )
            for user, codename in (
                # It seems unnecessary to filter assignments like this since a
                # user who can change assignments can also view all
                # assignments. Maybe that will change in the future, though?
                get_user_permission_assignments_queryset(
                    asset, self.context['request'].user
                ).exclude(user=asset.owner)
            ).values_list('user', 'permission__codename')
        )

        # Expand the stupid cache to include any users present in existing
        # assignments but not incoming assignments
        for uncached_user in User.objects.filter(
            pk__in=set(
                user_pk for user_pk, _, _ in existing_assignments
            ).difference(user_pk_to_obj.keys())
        ):
            user_pk_to_obj[uncached_user.pk] = uncached_user

        # Perform the removals
        for removal in existing_assignments.difference(incoming_assignments):
            user_pk, codename, _ = removal
            asset.remove_perm(user_pk_to_obj[user_pk], codename)
            print('---', user_pk_to_obj[user_pk].username, codename, flush=True)

        # Perform the new assignments
        for addition in incoming_assignments.difference(existing_assignments):
            user_pk, codename, partial_perms = addition
            if asset.owner_id == user_pk:
                raise serializers.ValidationError(
                    {'user': t(ASSIGN_OWNER_ERROR_MESSAGE)}
                )
            if partial_perms:
                partial_perms = json.loads(partial_perms)
            perm = asset.assign_perm(
                user_obj=user_pk_to_obj[user_pk],
                perm=codename,
                partial_perms=partial_perms,
            )
            print('+++', perm, partial_perms, flush=True)

        # Return nothing, in a nice way, because the view is responsible for
        # calling `list()` to return the assignments as they actually exist in
        # the database. That causes duplicate queries but at least ensures that
        # the front end displays accurate information even if there are bugs
        # here
        return {}

    def validate(self, attrs):
        # NOCOMMIT generally the point is that running the permission assignment
        # serializer repeatedly would query the database over and over again
        # for each user as well as each permission
        usernames = []
        codenames = []
        partial_codenames = []
        # Loop on POST data (i.e. `attrs['assignments']`) to retrieve code names,
        # and usernames. We use lists and not sets because we do need to keep
        # duplicates and the order for later process.
        for assignment in attrs['assignments']:
            codename = self._get_permission_codename(assignment['permission'])
            codenames.append(codename)
            if codename == PERM_PARTIAL_SUBMISSIONS:
                for partial_assignment in assignment['partial_permissions']:
                    partial_codenames.append(
                        self._get_permission_codename(partial_assignment['url'])
                    )

            usernames.append(self._get_username(assignment['user']))

        # Validate if code names and usernames are valid and retrieve
        # all users and permissions related to `codenames` and `usernames`
        self._validate_codenames(
            partial_codenames, suffix=SUFFIX_SUBMISSIONS_PERMS
        )
        users = self._validate_usernames(usernames)
        permissions = self._validate_codenames(codenames)

        # Double check that we have as many code names and usernames as
        # permission assignments. If it is not the case, something went south in
        # validation.
        assert len(codenames) == len(usernames) == len(attrs['assignments'])
        # NOCOMMIT other data structure ideas?

        assignment_objects = []
        # Loop on POST data to convert all URLs to their objects counterpart
        for idx, assignment in enumerate(attrs['assignments']):
            # As already said above, the positions in `usernames`, `codenames`
            # should (and must) be the same as in `attrs['assignments']`.
            assignment_object = {
                # NOCOMMIT _get_object is not a DRF thing
                'user': self._get_object(usernames[idx], users, 'username'),
                'permission': self._get_object(codenames[idx], permissions, 'codename')
            }
            if codenames[idx] == PERM_PARTIAL_SUBMISSIONS:
                assignment_object['partial_permissions'] = defaultdict(list)
                for partial_perms in assignment['partial_permissions']:
                    # Because, we kept the same order at the beginning, the
                    # first occurrence of `partial_codenames[0]` always belongs
                    # to the user we are processing in `assignment_object`.
                    partial_codename = partial_codenames.pop(0)
                    assignment_object['partial_permissions'][
                        partial_codename
                    ] = partial_perms['filters']

            assignment_objects.append(assignment_object)

        # Replace 'assignments' property with converted objects
        # Useful to process data when calling `.create()`
        attrs['assignments'] = assignment_objects

        return attrs

    def _get_permission_codename(self, permission_url: str) -> str:
        """
        Retrieve the code name with reverse matching of the permission URL
        """
        try:
            resolver_match = absolute_resolve(permission_url)
            codename = resolver_match.kwargs['codename']
        except (TypeError, KeyError, Resolver404):
            # NOCOMMIT previously we returned `{"permission":["Invalid hyperlink - Object does not exist."]}``
            raise serializers.ValidationError(
                t('Invalid permission: `## permission_url ##`').format(
                    {'## permission_url ##': permission_url}
                )
            )

        return codename

    def _get_username(self, user_url: str) -> str:
        """
        Retrieve the username with reverse matching of the user URL
        """
        try:
            resolver_match = absolute_resolve(user_url)
            username = resolver_match.kwargs['username']
        except (TypeError, KeyError, Resolver404):
            # NOCOMMIT previously we returned `{"user":["Invalid hyperlink - Object does not exist."]}`
            raise serializers.ValidationError(
                t('Invalid user: `## user_url ##`').format(
                    {'## user_url ##': user_url}
                )
            )

        return username

    def _validate_codenames(self, codenames: list, suffix: str = None) -> list:
        """
        Return a list of Permission models matching `codenames`.

        # NOCOMMIT: update docstring because it actually does verify that the contents match, not just the count
        If number of distinct code names does not match the number of
        assignable permissions on the asset, some code names are invalid
        and an error is raised.
        """
        asset = self.context['asset']
        codenames = set(codenames)
        assignable_permissions = asset.get_assignable_permissions(
            with_partial=True
        )
        diff = codenames.difference(assignable_permissions)
        if diff:
            # NOCOMMIT consider whether or not the error message would be
            # helpful to someone if they are only allowed to submit URLs in the first place
            raise serializers.ValidationError(t('Invalid code names'))

        # NOCOMMIT rename perhaps or drop in case creating a queryset that never gets evaluated isn't so bad
        if suffix:
            # No need to return Permission objects when validating partial
            # permission code names
            return

        return Permission.objects.filter(codename__in=codenames)

    def _validate_usernames(self, usernames: list) -> list:
        """
        Return a list of User models matching `usernames`.

        If number of distinct usernames does not match the number of
        users returned by the QuerySet, some usernames are invalid
        and an error is raised.
        """
        usernames = set(usernames)
        # We need to convert to a list and keep results in memory in order to
        # pass it to `._get_object()`
        # It is not designed to support a ton of users but it should be safe
        # with reasonable quantity.
        users = list(
            User.objects.only('pk', 'username').filter(username__in=usernames)
        )
        if len(users) != len(usernames):
            # NOCOMMIT: from the HTTP requestor's perspective, the users were passed by URL
            raise serializers.ValidationError(t('Invalid usernames'))

        return users

    def _get_object(
        self, value: str, object_list: list, fieldname: str
    ) -> Union[User, Permission]:
        """
        Search for a match in `object_list` where the value of
        property `fieldname` equals `value`

        `value` should be a unique identifier
        """
        for obj in object_list:
            if value == getattr(obj, fieldname):
                return obj
