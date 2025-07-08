import logging
from datetime import date
from typing import Dict, List, Optional

from django.db.models import QuerySet, Q, F
from django.db.models.functions import ExtractDay
from rest_access_policy import AccessPolicy
from rest_framework.fields import Field
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request

from core.models.models import User, ACTEDArea, Country
from core.utilities import rgetattr
from logistics.access_policies.common import AccessPolicyAction
from logistics.models.assets import (
    AssetUserAccess, Asset, AssetAllocationProjectContract, AssetAllocationPremises, AssetAllocationUsage, Inventory,
    InventoryAssetRelation, DisposalPlan, AssetMaintenance,
)
from logistics.models.confirmations import Confirmation
from security.models import Premises
from workflows.models import Transition

logger = logging.getLogger(__name__)


class AssetUserAccessAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_assetuseraccess'],
        }, {
            'action': ['create_many'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_assetuseraccess'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetuseraccess', 'can_delete_instance'],
        }, {
            'action': ['delete_many'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetuseraccess'],
        },
    ]

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[AssetUserAccess] = None) -> QuerySet[AssetUserAccess]:
        return cls.get_qs_for_user(request.user, qs)

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[AssetUserAccess] = None, action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[AssetUserAccess]:
        if not user:
            return AssetUserAccess.objects.none()
        if queryset is None:
            queryset = AssetUserAccess.objects.all()

        queryset = queryset.select_related(
            'user',
            'country',
            'acted_area',
        ).defer(
            'country__geom',
            'acted_area__geom',
        )

        # Users with permission view_assetuseraccess_all can see all accesses.
        if action == AccessPolicyAction.READ and user.has_perm('view_assetuseraccess_all'):
            return queryset
        # Users with permission delete_assetuseraccess_all can see all accesses.
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_assetuseraccess_all'):
            return queryset

        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = {}

            # Restrict per area and country.
            if user_access.acted_area:
                clauses['acted_area'] = user_access.acted_area
            if user_access.country:
                clauses['country'] = user_access.country
            else:
                # Or all accesses if they have no country restriction.
                clauses['pk__isnull'] = False

            # Non CONTROLLER/MANAGER can only access same role.
            if user_access.mode not in [AssetUserAccess.Mode.CONTROLLER, AssetUserAccess.Mode.MANAGER]:
                clauses['mode__in'] = [user_access.mode]
            else:
                # CONTROLLER role is only accessible by admin.
                clauses['mode__in'] = [mode for mode in AssetUserAccess.Mode if mode != AssetUserAccess.Mode.CONTROLLER]

            # OFFICER can see VALIDATORS.
            if action == AccessPolicyAction.READ and user_access.mode == AssetUserAccess.Mode.OFFICER:
                clauses['mode__in'] = [AssetUserAccess.Mode.OFFICER, AssetUserAccess.Mode.INVENTORY_VALIDATOR]

            # Add this user access
            if clauses:
                query.add(Q(**clauses), Q.OR)

        return queryset.filter(query).distinct() if query else AssetUserAccess.objects.none()

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user = request.user
        country_code = request.data.get('country', {}).get('code_iso')
        acted_area_code = request.data.get('acted_area', {}).get('code')
        mode = AssetUserAccess.Mode(request.data.get('mode'))
        return self.user_can_create_access(user, country_code, acted_area_code, mode)

    @classmethod
    def user_can_create_access(cls, user: User, country_code: str, acted_area_code: str | None, mode: AssetUserAccess.Mode) -> bool:
        # anonymous users can't create user accesses
        if not user:
            return False

        # Users with add_assetuseraccess_all permission can create any access (this permission is held by admin only).
        if user.has_perm('add_assetuseraccess_all'):
            return True
        # Only admin can create controller accesses.
        if mode == AssetUserAccess.Mode.CONTROLLER:
            return False

        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(acted_area__code=acted_area_code) | Q(acted_area__isnull=True),
            Q(country__code_iso=country_code) | Q(country__isnull=True),
            Q(mode=mode) | Q(mode__in=[AssetUserAccess.Mode.CONTROLLER, AssetUserAccess.Mode.MANAGER]),
        ).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        asset_user_access: AssetUserAccess = view.get_object()
        return self.user_can_delete_access(request.user, asset_user_access.id)

    @classmethod
    def user_can_delete_access(cls, user: User, user_access_id: int) -> bool:
        return cls.get_qs_for_user(user, action=AccessPolicyAction.DELETE).filter(id=user_access_id).exists()

    @classmethod
    def get_acted_areas_for_user(cls, user: User) -> QuerySet[ACTEDArea]:
        """
        Return a queryset of ACTEDAreas for which the given user has either a specific AssetUserAccess, or
          an access to the country with no specific area constraint.
        """
        base_qs = ACTEDArea.objects.filter(is_active=True).defer('geom')
        if user.has_perm('view_assetuseraccess_all'):
            return base_qs

        return ACTEDArea.objects.filter(
            # Get Areas the user has access to.
            Q(asset_user_accesses__user=user) |
            # Or Areas in countries the user has non-area specific access to.
            (
                    Q(mission_country__asset_user_accesses__user=user) &
                    Q(mission_country__asset_user_accesses__acted_area__isnull=True)
            ),
        )


class AssetAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log', 'suggest_accessories', 'fetch_external_data', 'export', 'export_usages_pdf'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition_expression': ['user_has_perm:view_asset or user_has_perm:view_asset_all'],
        }, {
            'action': ['update', 'partial_update', 'play_transition'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['can_update_instance'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['can_create_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['can_delete_instance'],
        },
    ]

    @classmethod
    def scope_fields(cls, request: Request, fields: Dict[str, Field], instance: Asset | List[Asset] | None = None) -> Dict[str, Field]:
        return cls.set_readonly_fields(instance, fields, request.user)

    @classmethod
    def set_readonly_fields(cls, asset: Asset | List[Asset] | None, fields: Dict[str, Field], user: User | None) -> Dict[str, Field]:
        # Don't change fields for list view.
        if asset is None or not isinstance(asset, Asset):
            return fields
        # Don't change fields for non Finance HQ users.
        if user is None or user.has_perm('change_asset') or user.has_perm('change_asset_all') or not user.has_perm('change_asset_purchase_number'):
            return fields

        editable_fields = ['purchase_number']
        for field in fields.keys():
            if field not in editable_fields:
                fields[field].read_only = True

        return fields

    @classmethod
    def scope_workflow_transition_check(cls, asset: Asset, transition: Transition, user: Optional[User]) -> bool:
        if user is None:
            return False

        # User with 'change_asset_all_transitions' (admin/machine user) can perform all transitions
        if user.has_perm('change_asset_all_transitions'):
            return True
        if transition.name == Asset.Transition.DISPOSE:
            return False

        country = asset.country_mission
        acted_area = asset.current_premises.place.acted_area if asset.current_premises is not None and asset.current_premises.place is not None else None
        logger.debug(f'{transition}/{user.username if user else None} → {country}/{acted_area}')
        for user_access in AssetAccessPolicy._get_accesses_for_country_and_area(user, country, acted_area):
            logger.debug(f'access: {user_access.mode}/{user_access.country}/{user_access.acted_area}')

            is_disposal_related = transition.name in [Asset.Transition.START_DISPOSAL, Asset.Transition.CANCEL_DISPOSAL]
            if user_access.mode in [AssetUserAccess.Mode.OFFICER, AssetUserAccess.Mode.MANAGER, AssetUserAccess.Mode.CONTROLLER]:
                if is_disposal_related:
                    return True

                # all the remaining transitions are possible for the 3 types of user
                allowed_transitions = [
                    Asset.Transition.ASSIGN,
                    Asset.Transition.ACCEPT_ASSIGN,
                    Asset.Transition.ACCEPT_UNASSIGN,
                    Asset.Transition.REJECT,
                    Asset.Transition.UNASSIGN,
                    Asset.Transition.SEND_MAINTENANCE,
                    Asset.Transition.END_MAINTENANCE_TO_STOCK,
                    Asset.Transition.SEND_REPAIR,
                    Asset.Transition.END_REPAIR_TO_STOCK,
                ]
                return transition.name in allowed_transitions

        return False

    @classmethod
    def _get_accesses_for_country_and_area(cls, user: User | None, country: Country | None, acted_area: ACTEDArea | None) -> QuerySet[AssetUserAccess]:
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(acted_area__isnull=True) | Q(acted_area=acted_area),
            Q(country__isnull=True) | Q(country=country),
        )

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[Asset] = None) -> QuerySet[Asset]:
        return cls.get_qs_for_user(request.user, qs, action=AccessPolicyAction.READ)

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        country_id = request.data.get('country_mission', {}).get('id')
        return self.user_can_create_asset(user, country_id)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        asset: Asset = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=asset.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        asset: Asset = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=asset.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User | None, queryset: QuerySet[Asset] | None = None,
                        action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[Asset]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_asset') or user.has_perm('view_asset_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_asset') or user.has_perm('change_asset_all') or user.has_perm('change_asset_purchase_number'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_asset') or user.has_perm('delete_asset_all'))):
            return Asset.objects.none()

        if queryset is None:
            queryset = Asset.objects.all()

        queryset = queryset.select_related(
            'current_premises',
            'current_premises__place',
            'current_premises__place__acted_area',
            'current_premises__place__admin_zone_2',
            'current_premises__place__admin_zone_2__admin_zone_1',
            'current_premises__place__admin_zone_2__admin_zone_1__country',
            'current_project_contract',
            'current_logistics_budget_line',
            'current_logistics_budget_line__budget_line',
            'current_donor',
            'current_staff',
            'department',
            'country_mission',
            'currency',
        ).defer(
            'current_premises__place__acted_area__geom',
            'current_premises__place__admin_zone_2__geom',
            'current_premises__place__admin_zone_2__admin_zone_1__geom',
            'current_premises__place__admin_zone_2__admin_zone_1__country__geom',
            'country_mission__geom',
        ).annotate(
            days_until_warranty_end=ExtractDay(F('warranty_end_date') - date.today()),  # SI-3378
        )

        # No further filtering is done for read permission view_asset_all.
        if action == AccessPolicyAction.READ and user.has_perm('view_asset_all'):
            return queryset
        # Users with permission change_asset_all can change all.
        if action == AccessPolicyAction.EDIT and user.has_perm('change_asset_all'):
            return queryset
        # Users with permission delete_asset_all can delete all.
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_asset_all'):
            return queryset

        # Users with permission change_asset_purchase_number can change all assets (but only a specific field).
        if action == AccessPolicyAction.EDIT and user.has_perm('change_asset_purchase_number'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            if action == AccessPolicyAction.DELETE:
                # Only controllers and managers can delete an asset.
                if user_access.mode not in [AssetUserAccess.Mode.MANAGER, AssetUserAccess.Mode.CONTROLLER]:
                    clauses.add(Q(pk__isnull=True), Q.AND)
                else:
                    # Asset can only be deleted when in IN_STOCK state.
                    clauses.add(Q(state=Asset.State.IN_STOCK), Q.AND)

            # Validators are not allowed to EDIT/DELETE.
            if user_access.mode in [AssetUserAccess.Mode.INVENTORY_VALIDATOR, AssetUserAccess.Mode.DISPOSAL_VALIDATOR] \
                    and action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
                clauses.add(Q(pk__isnull=True), Q.AND)

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(current_premises__place__acted_area=user_access.acted_area) | Q(current_premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(country_mission=user_access.country) | Q(country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else Asset.objects.none()

    @classmethod
    def user_can_create_asset(cls, user: User | None, country_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country.
        Acted area is omitted as it is not known at creation time.
        """
        if user is None or not (user.has_perm('add_asset') or user.has_perm('add_asset_all')):
            return False
        # Users with add_asset_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_asset_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(country__isnull=True) | Q(country__id=country_id),
        ).exists()


class AssetAllocationProjectContractAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_assetallocationprojectcontract'],
        }, {
            'action': ['update', 'partial_update'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_assetallocationprojectcontract', 'can_update_instance'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_assetallocationprojectcontract', 'can_create_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetallocationprojectcontract', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[AssetAllocationProjectContract] = None) -> QuerySet[AssetAllocationProjectContract]:
        return cls.get_qs_for_user(request.user, qs, action=AccessPolicyAction.READ)

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        asset = Asset.objects.get(id=request.data.get('asset'))
        country_id = asset.country_mission.id
        acted_area_id = rgetattr(asset, 'current_premises.place.acted_area.id', None)
        return self.user_can_create_asset_allocation_project_contract(user, country_id, acted_area_id)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationProjectContract = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=allocation.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationProjectContract = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=allocation.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[AssetAllocationProjectContract] = None,
                        action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[AssetAllocationProjectContract]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_assetallocationprojectcontract') or user.has_perm('view_assetallocationprojectcontract_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_assetallocationprojectcontract') or user.has_perm('change_assetallocationprojectcontract_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_assetallocationprojectcontract') or user.has_perm('delete_assetallocationprojectcontract_all'))):
            return AssetAllocationProjectContract.objects.none()

        if queryset is None:
            queryset = AssetAllocationProjectContract.objects.all()

        # No further filtering is done for read permission view_assetallocationprojectcontract_all
        if action == AccessPolicyAction.READ and user.has_perm('view_assetallocationprojectcontract_all'):
            return queryset
        # Users with permission change_assetallocationprojectcontract_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_assetallocationprojectcontract_all'):
            return queryset
        # Users with permission delete_assetallocationprojectcontract_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_assetallocationprojectcontract_all'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Officers can only EDIT/DELETE their own allocation.
            if user_access.mode in [AssetUserAccess.Mode.OFFICER] \
                    and action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
                clauses.add(Q(created_by=user), Q.AND)

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(asset__current_premises__place__acted_area=user_access.acted_area) | Q(asset__current_premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(asset__country_mission=user_access.country) | Q(asset__country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else AssetAllocationProjectContract.objects.none()

    @classmethod
    def user_can_create_asset_allocation_project_contract(cls, user: User | None, country_id: int | None, acted_area_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country and acted area.
        """
        if user is None or not (user.has_perm('add_assetallocationprojectcontract') or user.has_perm('add_assetallocationprojectcontract_all')):
            return False
        # Users with add_asset_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_assetallocationprojectcontract_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(acted_area__isnull=True) | Q(acted_area__id=acted_area_id),
            Q(country__isnull=True) | Q(country__id=country_id),
        ).exists()


class AssetAllocationPremisesAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_assetallocationpremises'],
        }, {
            'action': ['update', 'partial_update'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_assetallocationpremises', 'can_update_instance'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_assetallocationpremises', 'can_create_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetallocationpremises', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[AssetAllocationPremises] = None) -> QuerySet[AssetAllocationPremises]:
        return cls.get_qs_for_user(request.user, qs, action=AccessPolicyAction.READ)

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        asset = Asset.objects.get(id=request.data.get('asset', {}).get('id'))
        country_id = asset.country_mission.id
        acted_area_id = rgetattr(asset, 'current_premises.place.acted_area.id', None)
        return self.user_can_create_asset_allocation_premises(user, country_id, acted_area_id)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationPremises = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=allocation.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationPremises = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=allocation.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[AssetAllocationPremises] = None,
                        action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[AssetAllocationPremises]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_assetallocationpremises') or user.has_perm('view_assetallocationpremises_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_assetallocationpremises') or user.has_perm('change_assetallocationpremises_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_assetallocationpremises') or user.has_perm('delete_assetallocationpremises_all'))):
            return AssetAllocationPremises.objects.none()

        if queryset is None:
            queryset = AssetAllocationPremises.objects.all()

        queryset = queryset.select_related(
            'asset',
            'asset__current_premises',
            'premises',
            'premises__place',
            'premises__place__acted_area',
        ).defer(
            'premises__geom',
            'premises__place__geom',
            'premises__place__acted_area__geom',
        )

        # No further filtering is done for read permission view_assetallocationpremises_all
        if action == AccessPolicyAction.READ and user.has_perm('view_assetallocationpremises_all'):
            return queryset
        # Users with permission change_assetallocationpremises_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_assetallocationpremises_all'):
            return queryset
        # Users with permission delete_assetallocationpremises_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_assetallocationpremises_all'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Officers can only EDIT/DELETE allocations with WAITING confirmation.
            if user_access.mode in [AssetUserAccess.Mode.OFFICER] \
                    and action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
                clauses.add(Q(confirmations__type=Confirmation.Type.ASSET_TRANSFER_PREMISES, confirmations__state=Confirmation.State.WAITING), Q.AND)

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(asset__current_premises__place__acted_area=user_access.acted_area) | Q(asset__current_premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(asset__country_mission=user_access.country) | Q(asset__country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else AssetAllocationPremises.objects.none()

    @classmethod
    # def user_can_create_asset_allocation_project_contract(cls, user: User | None, asset: Asset | None -> bool:
    def user_can_create_asset_allocation_premises(cls, user: User | None, country_id: int | None, acted_area_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country and acted area.
        """
        if user is None or not (user.has_perm('add_assetallocationpremises') or user.has_perm('add_assetallocationpremises_all')):
            return False
        # Users with add_assetallocationpremises_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_assetallocationpremises_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(acted_area__isnull=True) | Q(acted_area__id=acted_area_id),
            Q(country__isnull=True) | Q(country__id=country_id),
        ).exists()


class AssetAllocationUsageAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_assetallocationusage'],
        }, {
            'action': ['update', 'partial_update'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_assetallocationusage', 'can_update_instance'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_assetallocationusage', 'can_create_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetallocationusage', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[AssetAllocationUsage] = None) -> QuerySet[AssetAllocationUsage]:
        return cls.get_qs_for_user(request.user, qs, action=AccessPolicyAction.READ)

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        asset = Asset.objects.get(id=request.data.get('asset', {}).get('id'))
        country_id = asset.country_mission.id
        acted_area_id = rgetattr(asset, 'current_premises.place.acted_area.id', None)
        return self.user_can_create_asset_allocation_usage(user, country_id, acted_area_id)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationPremises = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=allocation.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        allocation: AssetAllocationPremises = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=allocation.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[AssetAllocationUsage] = None,
                        action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[AssetAllocationUsage]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_assetallocationusage') or user.has_perm('view_assetallocationusage_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_assetallocationusage') or user.has_perm('change_assetallocationusage_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_assetallocationusage') or user.has_perm('delete_assetallocationusage_all'))):
            return AssetAllocationUsage.objects.none()

        if queryset is None:
            queryset = AssetAllocationUsage.objects.all()

        # No further filtering is done for read permission view_assetallocationusage_all
        if action == AccessPolicyAction.READ and user.has_perm('view_assetallocationusage_all'):
            return queryset
        # Users with permission change_assetallocationusage_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_assetallocationusage_all'):
            return queryset
        # Users with permission delete_assetallocationusage_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_assetallocationusage_all'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(asset__current_premises__place__acted_area=user_access.acted_area) | Q(asset__current_premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(asset__country_mission=user_access.country) | Q(asset__country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else AssetAllocationUsage.objects.none()

    @classmethod
    def user_can_create_asset_allocation_usage(cls, user: User | None, country_id: int | None, acted_area_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country and acted area.
        """
        if user is None or not (user.has_perm('add_assetallocationusage') or user.has_perm('add_assetallocationusage_all')):
            return False
        # Users with add_assetallocationusage_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_assetallocationusage_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(acted_area__isnull=True) | Q(acted_area__id=acted_area_id),
            Q(country__isnull=True) | Q(country__id=country_id),
        ).exists()


class InventoryAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log', 'export_pdf', 'export_inventories'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_inventory'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_inventory', 'can_create_instance'],
        }, {
            'action': ['update', 'partial_update', 'play_transition'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_inventory', 'can_update_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_inventory', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_workflow_transition_check(cls, inventory: Inventory, transition: Transition, user: User | None) -> bool:
        if user is None:
            return False

        # User with 'change_inventory_all_transitions' (admin/machine user) can perform all transitions.
        if user.has_perm('change_inventory_all_transitions'):
            return True

        country = inventory.premises.mission_country
        acted_area = inventory.premises.place.acted_area if inventory.premises.place is not None else None
        logger.debug(f'{transition}/{user.username if user else None} → {country}/{acted_area}')
        for user_access in InventoryAccessPolicy._get_accesses_for_country_and_area(user, country, acted_area):
            logger.debug(f'access: {user_access.mode}/{user_access.country}/{user_access.acted_area} → {transition.name}')
            # Validators can validate and reject.
            if user_access.mode in [AssetUserAccess.Mode.INVENTORY_VALIDATOR] \
                    and transition.name in [Inventory.Transition.TO_CORRECT, Inventory.Transition.VALIDATE]:
                return True
            # Officers can submit.
            if user_access.mode in [AssetUserAccess.Mode.OFFICER] and transition.name in [Inventory.Transition.SUBMIT]:
                return True
            # Managers and controllers can do all transitions.
            if user_access.mode in [AssetUserAccess.Mode.MANAGER, AssetUserAccess.Mode.CONTROLLER]:
                return True
        return False

    @classmethod
    def _get_accesses_for_country_and_area(cls, user: User | None, country: Country | None, acted_area: ACTEDArea | None) -> QuerySet[AssetUserAccess]:
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(acted_area__isnull=True) | Q(acted_area=acted_area),
            Q(country__isnull=True) | Q(country=country),
        )

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[Inventory] = None) -> QuerySet[Inventory]:
        return cls.get_qs_for_user(request.user, qs)

    def can_create_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        premises = Premises.objects.get(id=request.data.get('premises', {}).get('id'))
        country_id = premises.mission_country.id
        acted_area_id = rgetattr(premises, 'place.acted_area.id', None)
        return self.user_can_create_inventory(user, country_id, acted_area_id)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        inventory: Inventory = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=inventory.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        inventory: Inventory = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=inventory.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[Inventory] = None, action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[Inventory]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_inventory') or user.has_perm('view_inventory_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_inventory') or user.has_perm('change_inventory_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_inventory') or user.has_perm('delete_inventory_all'))):
            return Inventory.objects.none()

        if queryset is None:
            queryset = Inventory.objects.all()

        queryset = queryset.select_related(
            'premises',
            'premises__mission_country',
            'premises__place',
            'premises__place__admin_zone_2',
            'premises__place__admin_zone_2__admin_zone_1',
            'premises__place__admin_zone_2__admin_zone_1__country',
        ).defer(
            'premises__mission_country__geom',
            'premises__place__admin_zone_2__geom',
            'premises__place__admin_zone_2__admin_zone_1__geom',
            'premises__place__admin_zone_2__admin_zone_1__country__geom',
        ).prefetch_related(
            'inventory_asset_relations',
            'inventory_asset_relations__asset__current_staff',
            'inventory_asset_relations__asset__current_premises',
            'inventory_asset_relations__asset__current_premises__place',
            'inventory_asset_relations__room',
            'inventory_asset_relations__transferred_from',
            'inventory_asset_relations__transferred_from__place',
        )

        # Can't edit/delete a VALIDATED Inventory.
        if action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
            queryset = queryset.filter(~Q(state=Inventory.State.VALIDATED))

        # No further filtering is done for read permission view_inventory_all
        if action == AccessPolicyAction.READ and user.has_perm('view_inventory_all'):
            return queryset
        # Users with permission change_inventory_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_inventory_all'):
            return queryset
        # Users with permission delete_inventory_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_inventory_all'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Officers can only edit/delete ON_GOING Inventories.
            if user_access.mode == AssetUserAccess.Mode.OFFICER and action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
                clauses.add(Q(state=Inventory.State.ON_GOING), Q.AND)

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(premises__place__acted_area=user_access.acted_area) | Q(premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(premises__mission_country=user_access.country) | Q(premises__mission_country__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else Inventory.objects.none()

    @classmethod
    def user_can_create_inventory(cls, user: User | None, country_id: int | None, acted_area_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country and acted area.
        """
        if user is None or not (user.has_perm('add_inventory') or user.has_perm('add_inventory_all')):
            return False
        # Users with add_inventory_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_inventory_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.INVENTORY_VALIDATOR,
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(acted_area__isnull=True) | Q(acted_area__id=acted_area_id),
            Q(country__isnull=True) | Q(country__id=country_id),
        ).exists()


class InventoryAssetRelationAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_inventory', 'can_create_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_inventory', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[InventoryAssetRelation] = None) -> QuerySet[InventoryAssetRelation]:
        return cls.get_qs_for_user(request.user, qs)

    @staticmethod
    def can_create_instance(request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        inventory = Inventory.objects.get(id=request.data.get('inventory', {}).get('id'))
        return InventoryAccessPolicy.get_qs_for_user(user, action=AccessPolicyAction.EDIT).filter(id=inventory.id).exists()

    @staticmethod
    def can_delete_instance(request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        relation: InventoryAssetRelation = view.get_object()
        return InventoryAccessPolicy.get_qs_for_user(user, action=AccessPolicyAction.EDIT).filter(id=relation.inventory.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[InventoryAssetRelation] = None, action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[InventoryAssetRelation]:
        if queryset is None:
            queryset = InventoryAssetRelation.objects.all()

        queryset = queryset.select_related(
            'asset',
            'asset__current_staff',
            'asset__current_premises',
            'asset__current_premises__place',
            'room',
            'transferred_from',
            'transferred_from__place',
        )

        allowed_inventories = InventoryAccessPolicy.get_qs_for_user(user, action=action)
        return queryset.filter(inventory__in=allowed_inventories).distinct()


class DisposalPlanAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log', 'list_attachments', 'download_attachment', 'make_donation_certificate'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_disposalplan'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_disposalplan', 'can_create_instance'],
        }, {
            'action': ['play_transition'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['can_transition_instance'],
        }, {
            'action': ['update', 'partial_update', 'create_attachments', 'delete_attachment'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_disposalplan', 'can_update_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_disposalplan', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_fields(cls, request: Request, fields: Dict[str, Field], instance: DisposalPlan | List[DisposalPlan] | None = None) -> Dict[str, Field]:
        if instance is None or isinstance(instance, List):
            return fields
        return cls.set_read_only_fields(instance, fields, request.user)

    @classmethod
    def set_read_only_fields(cls, disposal_plan: DisposalPlan, fields: Dict[str, Field], user: User | None) -> Dict[str, Field]:
        read_only_fields: List[str] = []
        if disposal_plan.state not in [DisposalPlan.State.DRAFT, DisposalPlan.State.UNDER_MANAGER_VALIDATION]:
            read_only_fields += [
                'reason', 'removal_plan', 'recycling_method',
                'reimbursed_amount', 'reimbursed_currency',
                'recipient_name',
            ]
        if disposal_plan.state == DisposalPlan.State.DONE:
            read_only_fields += ['disposed_date']

        for field_name in read_only_fields:
            fields[field_name].read_only = True
        return fields

    @classmethod
    def scope_workflow_transition_check(cls, disposal_plan: DisposalPlan, transition: Transition, user: User | None) -> bool:
        if user is None:
            return False

        # User with 'change_disposal_plan_all_transitions' (admin/machine user) can perform all transitions.
        if user.has_perm('change_disposal_plan_all_transitions'):
            return True

        # Admin can do all transitions except validating a submitted disposal plan.
        is_admin = user.has_perm('change_asset_all')
        if is_admin and transition.source_state.name not in [DisposalPlan.State.UNDER_FINANCE_VALIDATION]:
            return True

        country = disposal_plan.country_mission
        acted_area = disposal_plan.acted_area
        logger.debug(f'{transition}/{user.username if user else None} → {country}/{acted_area}')
        for user_access in AssetAccessPolicy._get_accesses_for_country_and_area(user, country, acted_area):
            logger.debug(f'access: {user_access.mode}/{user_access.country}/{user_access.acted_area}')
            if transition.source_state.name == DisposalPlan.State.UNDER_FINANCE_VALIDATION:
                return user_access.mode in [AssetUserAccess.Mode.DISPOSAL_VALIDATOR]
            if transition.name in [DisposalPlan.Transition.SUBMIT_TO_FINANCE, DisposalPlan.Transition.TO_CORRECT]:
                if user_access.mode in [AssetUserAccess.Mode.OFFICER]:
                    return False
            if user_access.mode in [AssetUserAccess.Mode.OFFICER, AssetUserAccess.Mode.MANAGER, AssetUserAccess.Mode.CONTROLLER]:
                return True
        return False

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[DisposalPlan] = None) -> QuerySet[DisposalPlan]:
        return cls.get_qs_for_user(request.user, qs)

    @staticmethod
    def can_create_instance(request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        asset_data = request.data.get('assets', [])
        asset_ids = [a.get('id') for a in asset_data]
        return DisposalPlanAccessPolicy.user_can_create_disposal_plan(user, asset_ids)

    def can_update_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        disposal_plan: DisposalPlan = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=disposal_plan.id).exists()

    def can_transition_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        disposal_plan: DisposalPlan = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.PLAY_TRANSITION).filter(id=disposal_plan.id).exists()

    def can_delete_instance(self, request: Request, view: GenericAPIView, action: str) -> bool:
        disposal_plan: DisposalPlan = view.get_object()
        return self.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=disposal_plan.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User, queryset: QuerySet[DisposalPlan] = None, action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[DisposalPlan]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_disposalplan') or user.has_perm('view_disposalplan_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_disposalplan') or user.has_perm('change_disposalplan_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_disposalplan') or user.has_perm('delete_disposalplan_all'))):
            return DisposalPlan.objects.none()

        if queryset is None:
            queryset = DisposalPlan.objects.all()

        queryset = queryset.select_related(
            'reimbursed_currency',
            'country_mission',
            'acted_area',
        ).defer(
            'country_mission__geom',
            'acted_area__geom',
        )

        # Can't edit/delete a DONE DisposalPlan.
        if action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
            queryset = queryset.filter(~Q(state=DisposalPlan.State.DONE))

        # No further filtering is done for read permission view_disposalplan_all
        if action == AccessPolicyAction.READ and user.has_perm('view_disposalplan_all'):
            return queryset
        # Users with permission change_disposalplan_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_disposalplan_all'):
            return queryset
        # Users with permission change_disposalplan_all can do all transition except validating and submitting.
        if action == AccessPolicyAction.PLAY_TRANSITION and user.has_perm('change_disposalplan_all'):
            return queryset.filter(~Q(state=DisposalPlan.State.UNDER_FINANCE_VALIDATION))
        # Users with permission delete_disposalplan_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_disposalplan_all'):
            return queryset

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Disposal validators can only read and play transitions.
            if user_access.mode == AssetUserAccess.Mode.DISPOSAL_VALIDATOR and action not in [AccessPolicyAction.READ, AccessPolicyAction.PLAY_TRANSITION]:
                clauses.add(Q(pk__isnull=True), Q.AND)

            # Can't delete non DRAFT DisposalPlan.
            if action == AccessPolicyAction.DELETE:
                clauses.add(Q(state=DisposalPlan.State.DRAFT), Q.AND)

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(acted_area=user_access.acted_area) | Q(acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(country_mission=user_access.country) | Q(country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else DisposalPlan.objects.none()

    @staticmethod
    def user_can_create_disposal_plan(user, assets: List[int]) -> bool:
        # User can create disposal plan if:
        # - he has add_disposalplan permission;
        # - he can modify related assets.
        if user is None:
            return False
        if not user.has_perm('add_disposalplan'):
            return False

        for asset_id in assets:
            can_modify_asset = AssetAccessPolicy.get_qs_for_user(user, action=AccessPolicyAction.EDIT).filter(id=asset_id).exists()
            if not can_modify_asset:
                return False
        return len(assets) > 0  # Ensure there's at least one asset.


class AssetMaintenanceAccessPolicy(AccessPolicy):
    statements = [
        {
            'action': ['list', 'retrieve', 'get_audit_log', 'list_attachments', 'download_attachment'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:view_assetmaintenance'],
        }, {
            'action': ['create'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:add_assetmaintenance', 'can_create_instance'],
        }, {
            'action': ['update', 'partial_update', 'create_attachments', 'delete_attachment', 'play_transition'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:change_assetmaintenance', 'can_update_instance'],
        }, {
            'action': ['destroy'],
            'principal': ['authenticated'],
            'effect': 'allow',
            'condition': ['user_has_perm:delete_assetmaintenance', 'can_delete_instance'],
        },
    ]

    @classmethod
    def scope_fields(cls, request: Request, fields: Dict[str, Field], instance: AssetMaintenance | List[AssetMaintenance] | None = None) -> Dict[str, Field]:
        if instance is None or not isinstance(instance, AssetMaintenance):
            return fields
        if instance.id is not None:
            fields['type'].read_only = True
        return fields

    @classmethod
    def scope_queryset(cls, request: Request, qs: QuerySet[AssetMaintenance] = None) -> QuerySet[AssetMaintenance]:
        return cls.get_qs_for_user(request.user, qs)

    @classmethod
    def can_create_instance(cls, request: Request, view: GenericAPIView, action: str) -> bool:
        user: User = request.user
        asset = Asset.objects.get(id=request.data.get('asset', {}).get('id'))
        country_id = asset.country_mission.id
        acted_area_id = rgetattr(asset, 'current_premises.place.acted_area.id', None)
        return cls.user_can_create_asset_maintenance(user, country_id, acted_area_id)

    @classmethod
    def can_update_instance(cls, request: Request, view: GenericAPIView, action: str) -> bool:
        asset_maintenance: AssetMaintenance = view.get_object()
        return cls.get_qs_for_user(request.user, action=AccessPolicyAction.EDIT).filter(id=asset_maintenance.id).exists()

    @classmethod
    def can_delete_instance(cls, request: Request, view: GenericAPIView, action: str) -> bool:
        asset_maintenance: AssetMaintenance = view.get_object()
        return cls.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=asset_maintenance.id).exists()

    @classmethod
    def get_qs_for_user(cls, user: User | None, queryset: QuerySet[AssetMaintenance] | None = None, action: AccessPolicyAction = AccessPolicyAction.READ) -> QuerySet[AssetMaintenance]:
        if user is None \
                or (action == AccessPolicyAction.READ and not (user.has_perm('view_assetmaintenance') or user.has_perm('view_assetmaintenance_all'))) \
                or (action == AccessPolicyAction.EDIT and not (user.has_perm('change_assetmaintenance') or user.has_perm('change_assetmaintenance_all'))) \
                or (action == AccessPolicyAction.DELETE and not (user.has_perm('delete_assetmaintenance') or user.has_perm('delete_assetmaintenance_all'))):
            return AssetMaintenance.objects.none()

        if queryset is None:
            queryset = AssetMaintenance.objects.all()

        queryset = queryset.select_related(
            'asset',
            'currency',
        )

        # No further filtering is done for read permission view_assetmaintenance_all
        if action == AccessPolicyAction.READ and user.has_perm('view_assetmaintenance_all'):
            return queryset
        # Users with permission change_assetmaintenance_all can change all
        if action == AccessPolicyAction.EDIT and user.has_perm('change_assetmaintenance_all'):
            return queryset
        # Users with permission delete_assetmaintenance_all can delete all
        if action == AccessPolicyAction.DELETE and user.has_perm('delete_assetmaintenance_all'):
            return queryset
        # Non-admins can't edit/delete a DONE AssetMaintenance.
        if action in [AccessPolicyAction.EDIT, AccessPolicyAction.DELETE]:
            queryset = queryset.filter(~Q(state=AssetMaintenance.State.DONE))

        # Otherwise need to filter according to AssetUserAccess.
        query = Q()
        for user_access in AssetUserAccess.objects.filter(user=user):
            clauses = Q()

            # Filter by acted area if specified.
            if user_access.acted_area is not None:
                clauses.add(Q(asset__current_premises__place__acted_area=user_access.acted_area) | Q(asset__current_premises__place__acted_area__isnull=True), Q.AND)
            # Filter by country if specified.
            if user_access.country is not None:
                clauses.add(Q(asset__country_mission=user_access.country) | Q(asset__country_mission__isnull=True), Q.AND)
            else:
                clauses.add(Q(pk__isnull=False), Q.AND)

            # Add this user access.
            query.add(clauses, Q.OR)

        return queryset.filter(query).distinct() if query else AssetMaintenance.objects.none()

    @classmethod
    def user_can_create_asset_maintenance(cls, user: User | None, country_id: int | None, acted_area_id: int | None) -> bool:
        """
        Return whether the user can create an instance for this country.
        """
        if user is None or not (user.has_perm('add_assetmaintenance') or user.has_perm('add_assetmaintenance_all')):
            return False
        # Users with add_assetmaintenance_all permission can create without any AssetUserAccess instances.
        if user.has_perm('add_assetmaintenance_all'):
            return True

        allowed_roles = [
            AssetUserAccess.Mode.OFFICER,
            AssetUserAccess.Mode.MANAGER,
            AssetUserAccess.Mode.CONTROLLER,
        ]
        return AssetUserAccess.objects.filter(
            Q(user=user),
            Q(mode__in=allowed_roles),
            Q(country__isnull=True) | Q(country__id=country_id),
            Q(acted_area__isnull=True) | Q(acted_area__id=acted_area_id),
        ).exists()

    @classmethod
    def scope_workflow_transition_check(cls, asset_maintenance: AssetMaintenance, transition: Transition, user: User | None) -> bool:
        if user is None:
            return False

        # User with 'change_assetmaintenance_all_transitions' (admin/machine user) can perform all transitions.
        if user.has_perm('change_assetmaintenance_all_transitions'):
            return True

        # Users who can modify the asset maintenance, can do workflow transitions.
        return cls.get_qs_for_user(user, None, AccessPolicyAction.EDIT).filter(id=asset_maintenance.id).exists()
