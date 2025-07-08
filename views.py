from urllib.parse import quote

import logging
from urllib.parse import quote

from django.db import transaction
from django.db.models import QuerySet, F, Func
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _, get_language
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import mixins, viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError, PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_access_policy import AccessPolicy, AccessViewSetMixin

from attachments.mixins import AttachmentViewSetMixin
from comments.mixins import CommentViewSetMixin
from core.apis.microsoft import get_microsoft_graph_authentication_token
from core.mixins.audit_log_viewset_mixin import AuditLogViewSetMixin
from core.models.models import LongRunningJob, User, Country, ACTEDArea
from core.serializers.long_running_jobs import LongRunningJobSerializer
from elastic.documents import SupplierDocument
from elastic.mixins import SearchViewSetMixin
from finance.models import BudgetLine, FinancialSheetVersion, FinancialSheet
from grants_management.models import ProjectContract
from logistics.access_policies.assets import (
    AssetAccessPolicy, AssetAllocationProjectContractAccessPolicy, AssetAllocationPremisesAccessPolicy,
    AssetAllocationUsageAccessPolicy, AssetUserAccessAccessPolicy, InventoryAccessPolicy,
    InventoryAssetRelationAccessPolicy, DisposalPlanAccessPolicy, AssetMaintenanceAccessPolicy,
)
from logistics.access_policies.common import AccessPolicyAction
from logistics.access_policies.confirmations import ConfirmationAccessPolicy
from logistics.access_policies.phone_lines import PhoneLineAccessPolicy
from logistics.access_policies.procurements import (
    ProcurementMainAccessPolicy, ProcurementMainRelationAccessPolicy, LogisticsProjectContractAccessPolicy,
    LogisticsBudgetLineAccessPolicy, ProcurementPlanVersionAccessPolicy,
    WaiverAccessPolicy, WaiverRelationAccessPolicy, ProcurementUserAccessAccessPolicy,
)
from logistics.access_policies.suppliers import (
    SupplierAccessPolicy, SupplierUserAccessAccessPolicy, SupplierMemberAccessPolicy,
    SupplierSuspendHistoryAccessPolicy,
)
from logistics.apis.intune_api import get_intune_data, find_intune_id
from logistics.apis.lenovo_api import get_lenovo_data
from logistics.filters import (
    LogisticsProjectContractFilter, ProcurementPlanVersionFilter, LogisticsBudgetLineFilter, WaiverFilter,
    WaiverRelationFilter, SupplierFilter, SupplierUserAccessFilter, SupplierMemberFilter, ProcurementUserAccessFilter,
    SupplierSuspendHistoryFilter, AssetFilter, AssetAllocationProjectContractFilter, AssetAllocationPremisesFilter,
    AssetAllocationUsagesFilter, ConfirmationFilter, AssetUserAccessFilter, InventoryFilter,
    InventoryAssetRelationFilter, DisposalPlanFilter, AssetMaintenanceFilter, PhoneLineFilter,
)
from logistics.filters import ProcurementMainFilter, ProcurementMainRelationFilter
from logistics.models.assets import (
    DisposalPlan, Asset, AssetAllocationPremises, AssetAllocationProjectContract, AssetAllocationUsage, AssetUserAccess,
    Inventory, InventoryAssetRelation, AssetMaintenance,
)
from logistics.models.confirmations import Confirmation
from logistics.models.phone_lines import PhoneLine
from logistics.models.procurements import (
    ProcurementUserAccess, ProcurementPlanVersion, LogisticsBudgetLine, ProcurementMain, ProcurementMainRelation,
    Waiver, WaiverRelation,
)
from logistics.models.suppliers import SupplierSuspendHistory, SupplierUserAccess, SupplierMember, Supplier
from logistics.serializers.asset_user_accesses import AssetUserAccessSerializer
from logistics.serializers.assets import AssetListSerializer, AssetDetailSerializer
from logistics.serializers.assets_allocations_premises import AssetAllocationPremisesSerializer
from logistics.serializers.assets_allocations_project_contracts import AssetAllocationProjectContractSerializer
from logistics.serializers.assets_allocations_usages import AssetAllocationUsagesSerializer
from logistics.serializers.assets_maintenances import AssetMaintenanceSerializer
from logistics.serializers.confirmations import ConfirmationSerializer
from logistics.serializers.disposal_plans import DisposalPlanDetailSerializer, DisposalPlanListSerializer
from logistics.serializers.inventories import InventoryDetailSerializer, InventoryListSerializer
from logistics.serializers.inventory_asset_relations import InventoryAssetRelationSerializer
from logistics.serializers.logistics_budget_lines import LogisticsBudgetLineSerializer
from logistics.serializers.logistics_projects import (
    LogisticsProjectContractDetailSerializer, LogisticsProjectContractListSerializer,
)
from logistics.serializers.phone_lines import PhoneLineSerializer
from logistics.serializers.procurement_plan_versions import ProcurementPlanVersionSerializer
from logistics.serializers.procurement_user_accesses import ProcurementUserAccessSerializer
from logistics.serializers.procurements_main import ProcurementMainSerializer
from logistics.serializers.procurements_main_relations import ProcurementMainRelationSerializer
from logistics.serializers.supplier_members import SupplierMemberListSerializer, SupplierMemberDetailSerializer
from logistics.serializers.supplier_suspend_histories import SupplierSuspendHistorySerializer
from logistics.serializers.supplier_user_accesses import SupplierUserAccessSerializer
from logistics.serializers.suppliers import SupplierDetailSerializer, SupplierListSerializer
from logistics.serializers.waiver_relations import WaiverRelationSerializer
from logistics.serializers.waivers import WaiverSerializer
from logistics.tasks.assets_inventory_export import EXPORT_TYPE_PERIOD, EXPORT_TYPE_PROJECT
from logistics.tasks.assets_export import export_assets
from logistics.tasks.assets_inventory_export import export_assets_inventory, export_assets_inventories
from logistics.tasks.assets_usages_export import export_asset_usages
from logistics.tasks.phone_lines_export import export_phone_lines
from logistics.tasks.procurement_plan_import import import_procurement_plan
from logistics.tasks.suppliers_export import export_suppliers
from logistics.tasks.suppliers_import import import_suppliers
from transparency.mixins import CertificationViewSetMixin
from workflows.mixins import WorkflowViewSetMixin
from workflows.models import Workflow

logger = logging.getLogger(__name__)


class ProcurementAccessPolicyViewSet(AccessViewSetMixin, mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = ProcurementUserAccessSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = ProcurementUserAccessFilter
    ordering_fields = [f.name for f in ProcurementUserAccess._meta.fields] + [
        'country__label_en',
        'country__label_fr',
        'user__username',
        'user__email',
    ]
    access_policy = ProcurementUserAccessAccessPolicy

    def get_queryset(self) -> QuerySet[ProcurementUserAccess]:
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='create-many')
    def create_many(self, request: Request):
        """
        Create multiple UserAccess instances from the incoming payload,
        i.e. multiple Countries for the given User.
        """
        with transaction.atomic():
            username = request.data.get('user', {}).get('username')
            country_codes = request.data.get('country_codes')
            mode = request.data.get('mode')

            if not all([username, mode]):
                raise ValidationError({'__all__': [_('COMMON__MISSING_FIELDS')]})

            user = User.objects.get(username=username)

            for country_code in country_codes or [None]:
                country = Country.objects.filter(code_iso=country_code).first()

                can_create = self.access_policy.user_can_create_access(request.user, country, mode)
                if not can_create:
                    raise PermissionDenied()

                access, _created = ProcurementUserAccess.objects.get_or_create(
                    user=user,
                    country=country,
                    defaults={'mode': mode},
                )
                access.mode = mode
                access.save()

        return Response(status=status.HTTP_201_CREATED)

    @action(methods=['POST'], detail=False, url_path='delete-many')
    def delete_many(self, request):
        """
        Delete multiple UserAccess instances from the incoming payload.
        """
        with transaction.atomic():
            num_deleted = 0

            # Loop over the accesses to delete.
            for access_id in request.data.get('access_ids') or []:
                user_access: ProcurementUserAccess = ProcurementUserAccess.objects.filter(id=access_id).first()
                if user_access:
                    # The definition of whether the user has the right to delete the accesses is whether they had the
                    # right to create them in the first place
                    user_can_create = self.access_policy.user_can_create_access(request.user, user_access.country, user_access.mode)
                    if user_can_create:
                        user_access.delete()
                        num_deleted += 1

        return Response({'num_deleted': num_deleted}, status=status.HTTP_200_OK)


class LogisticsProjectViewSet(AccessPolicy, CommentViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    lookup_field = 'code_project'
    queryset = ProjectContract.objects.none()
    permission_classes = (LogisticsProjectContractAccessPolicy,)
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = LogisticsProjectContractFilter
    ordering_fields = [f.name for f in ProjectContract._meta.fields] + [
        'currency__name',
        'project__country__label_en',
        'project__country__label_fr',
        'donor__name',
        'donor_back__name',
    ]

    def get_queryset(self):
        return ProjectContract.get_qs_for_user(self.request.user)

    def get_serializer_class(self):
        return LogisticsProjectContractListSerializer if self.action == 'list' else LogisticsProjectContractDetailSerializer

    @action(methods=['GET'], detail=True, url_path='latest-budget-lines')
    def get_latest_budget_lines(self, request: Request, code_project: str):
        """
        Get latest logistics budget lines for given project.
        """
        contract: ProjectContract = self.get_object()

        latest_procurement_plan_version: ProcurementPlanVersion = ProcurementPlanVersion.objects.filter(
            financial_sheet_version__financial_sheet__project_contract=contract,
        ).order_by('-version_major', '-version_minor', '-version_plan').first()

        logistics_budget_lines: QuerySet[LogisticsBudgetLine] = LogisticsBudgetLineAccessPolicy.get_qs_for_user(request.user).filter(
            procurement_plan_version=latest_procurement_plan_version,
        ).order_by('budget_line__code')

        data = LogisticsBudgetLineSerializer(logistics_budget_lines, many=True).data
        return Response(data, status=status.HTTP_200_OK)


class ProcurementPlanVersionViewSet(AccessViewSetMixin, WorkflowViewSetMixin, mixins.UpdateModelMixin, mixins.ListModelMixin,
                                    mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = ProcurementPlanVersionAccessPolicy
    serializer_class = ProcurementPlanVersionSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = ProcurementPlanVersionFilter
    ordering_fields = [f.name for f in ProcurementPlanVersion._meta.fields] + ['project_contract__id']

    def get_queryset(self) -> QuerySet[ProcurementPlanVersion]:
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='import')
    def do_import(self, request: Request) -> Response:
        # Verify required parameters are provided.
        procurement_plan_version_file = self.request.FILES.get('theFile')
        financial_sheet_version_id = self.request.data.get('financialSheetVersionId')
        reception_date = self.request.data.get('receptionDate')
        if not procurement_plan_version_file or not financial_sheet_version_id:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # create the LRJ to drive the import, the targeted Project is included in the job detail
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.IMPORT_PROCUREMENT_PLAN,
            detail={'financial_sheet_version_id': financial_sheet_version_id, 'reception_date': reception_date},
        )
        # attach the import file to the import
        job.attach(procurement_plan_version_file.name, procurement_plan_version_file.read(), created_by=self.request.user)

        # launch the async task
        import_procurement_plan.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)


class LogisticsBudgetLineViewSet(AccessViewSetMixin, mixins.UpdateModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = LogisticsBudgetLineAccessPolicy
    serializer_class = LogisticsBudgetLineSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = LogisticsBudgetLineFilter
    ordering_fields = [f.name for f in LogisticsBudgetLine._meta.fields] + [
        'budget_line__code',
        'budget_line__budget_line_code',
        'budget_line__description',
        'budget_line__payer__name',
        'budget_line__type',
        'budget_line__amount_donor_local',
        'budget_line__amount_donor_euro',
        'budget_line__amount_donor_usd',
        'budget_line__amount_cofunding_local',
        'budget_line__amount_cofunding_euro',
        'budget_line__amount_cofunding_usd',
        'budget_line__amount_in_kind_local',
        'budget_line__amount_in_kind_euro',
        'budget_line__amount_in_kind_usd',
        'budget_line__amount_total_local',
        'budget_line__amount_total_euro',
        'budget_line__amount_total_usd',
    ]

    def get_queryset(self) -> QuerySet[LogisticsBudgetLine]:
        return self.access_policy.scope_queryset(self.request)


class ProcurementMainViewSet(AccessPolicy, WorkflowViewSetMixin, AuditLogViewSetMixin, viewsets.ModelViewSet):
    queryset = ProcurementMain.objects.all()
    permission_classes = (ProcurementMainAccessPolicy,)
    serializer_class = ProcurementMainSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = ProcurementMainFilter
    ordering_fields = [f.name for f in ProcurementMain._meta.fields]


class ProcurementMainRelationViewSet(AccessPolicy, mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.ListModelMixin,
                                     mixins.DestroyModelMixin, viewsets.GenericViewSet):
    queryset = ProcurementMainRelation.objects.select_related(
        'procurement',
        'logistics_budget_line',
        'logistics_budget_line__budget_line',
        'logistics_budget_line__budget_line__payer',
        'logistics_budget_line__budget_line__financial_sheet_version',
        'logistics_budget_line__budget_line__financial_sheet_version',
        'logistics_budget_line__budget_line__financial_sheet_version__financial_sheet',
        'logistics_budget_line__budget_line__financial_sheet_version__financial_sheet__project_contract',
        'logistics_budget_line__budget_line__financial_sheet_version__financial_sheet__project_contract__currency',
    )
    permission_classes = (ProcurementMainRelationAccessPolicy,)
    serializer_class = ProcurementMainRelationSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = ProcurementMainRelationFilter
    ordering_fields = [f.name for f in ProcurementMainRelation._meta.fields] + [
        'logistics_budget_line__{}'.format(f.name) for f in LogisticsBudgetLine._meta.fields
    ] + [
        'logistics_budget_line__budget_line__{}'.format(f.name) for f in BudgetLine._meta.fields
    ] + [
        'logistics_budget_line__budget_line__financial_sheet_version__{}'.format(f.name) for f in FinancialSheetVersion._meta.fields
    ] + [
        'logistics_budget_line__budget_line__financial_sheet_version__financial_sheet__{}'.format(f.name) for f in FinancialSheet._meta.fields
    ] + [
        'logistics_budget_line__budget_line__financial_sheet_version__financial_sheet__project_contract__{}'.format(f.name) for f in ProjectContract._meta.fields
    ]


class WaiverViewSet(AccessPolicy, WorkflowViewSetMixin, AuditLogViewSetMixin, mixins.ListModelMixin,
                    mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    lookup_field = 'code'
    queryset = Waiver.objects.all()
    permission_classes = (WaiverAccessPolicy,)
    serializer_class = WaiverSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = WaiverFilter
    ordering_fields = [f.name for f in Waiver._meta.fields] + ['project__code_project']


class WaiverRelationViewSet(AccessPolicy, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    queryset = WaiverRelation.objects.all()
    permission_classes = (WaiverRelationAccessPolicy,)
    serializer_class = WaiverRelationSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = WaiverRelationFilter
    ordering_fields = [f.name for f in WaiverRelation._meta.fields]


class SupplierMemberViewSet(AccessViewSetMixin, AttachmentViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin, mixins.ListModelMixin,
                            mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = SupplierMemberAccessPolicy
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = SupplierMemberFilter
    ordering_fields = [f.name for f in SupplierMember._meta.fields] + [
        'nationality__label_en',
        'nationality__label_fr',
        'nationality_2__label_en',
        'nationality_2__label_fr',
        'nationality_3__label_en',
        'nationality_3__label_fr',
        'country_of_residence__label_en',
        'country_of_residence__label_fr',
    ]

    def get_queryset(self) -> QuerySet[SupplierMember]:
        return self.access_policy.scope_queryset(self.request)

    def get_serializer_class(self):
        return SupplierMemberListSerializer if self.action == 'list' else SupplierMemberDetailSerializer


class SupplierViewSet(AccessViewSetMixin, AttachmentViewSetMixin, CertificationViewSetMixin, SearchViewSetMixin,
                      WorkflowViewSetMixin, AuditLogViewSetMixin, CommentViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      mixins.CreateModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'code'
    access_policy = SupplierAccessPolicy
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = SupplierFilter
    document_class = SupplierDocument
    ordering_fields = [f.name for f in Supplier._meta.fields] + [
        'country_request__label_en',
        'country_request__label_fr',
        'acted_area__name',
        'country_registration__label_en',
        'country_registration__label_fr',
        'latest_validated_certification_is_active',
    ]

    def get_serializer_class(self):
        return SupplierListSerializer if self.action == 'list' else SupplierDetailSerializer

    def get_queryset(self) -> QuerySet[Supplier]:
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='import')
    def do_import(self, request):
        # Only users with permission add_supplier_all can import.
        if not request.user or not request.user.has_perm('add_supplier_all'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if not self.request.FILES.get('theFile'):
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # create the LRJ to drive the import
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.IMPORT_SUPPLIERS,
        )
        # attach the import file to the import
        suppliers_file = self.request.FILES.get('theFile')
        job.attach(suppliers_file.name, suppliers_file.read(), created_by=self.request.user)

        # launch the async task
        import_suppliers.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)

    @action(methods=['POST'], detail=False, url_path='export')
    def export(self, request):
        # the export can be filtered according to the current webapp filters
        qs_filters = request.data.get('filters')

        # create the long-running job with the filters in its metadata
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_SUPPLIERS,
            detail={'filters': qs_filters},
        )
        # launch the async task
        export_suppliers.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)

    @action(methods=['POST'], detail=True, url_path='convert-to-organisation')
    def convert_to_organisation(self, request, code=None):
        supplier = get_object_or_404(self.get_queryset(), code=code)

        supplier.convert_individual_to_organisation()
        supplier.refresh_from_db()
        data = SupplierDetailSerializer(supplier, context={'request': request}).data
        return Response(data, status=status.HTTP_200_OK)


class SupplierUserAccessViewSet(AccessViewSetMixin, mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = SupplierUserAccessSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = SupplierUserAccessFilter
    ordering_fields = [
        'id',
        'country__label_en',
        'country__label_fr',
        'acted_area__name',
        'user__username',
        'user__email',
        'mode',
    ]
    access_policy = SupplierUserAccessAccessPolicy

    def get_queryset(self) -> QuerySet[SupplierUserAccess]:
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='create-many')
    def create_many(self, request: Request):
        """
        Create multiple SupplierUserAccess instances from the incoming payload,
        i.e. multiple ACTEDAreas for the given User.
        """
        with transaction.atomic():
            username = request.data.get('user', {}).get('username')
            mode = request.data.get('mode')
            country_code_iso = request.data.get('country', {}).get('code_iso')
            acted_areas = request.data.get('acted_areas', [])
            acted_area_codes = [area.get('code') for area in acted_areas] or [None]

            if not all([username, mode]):
                raise ValidationError({'__all__': ['missing fields']})

            user = User.objects.get(username=username)
            country = Country.objects.filter(code_iso=country_code_iso).first()

            for acted_area_code in acted_area_codes:
                acted_area = ACTEDArea.objects.filter(code=acted_area_code).first()

                can_create = self.access_policy.user_can_create_access(
                    request.user, country_code_iso, acted_area_code, mode,
                )

                if not can_create:
                    raise PermissionDenied()

                SupplierUserAccess.objects.create(
                    user=user,
                    mode=mode,
                    country=country,
                    acted_area=acted_area,
                )

        return Response(status=status.HTTP_201_CREATED)

    @action(methods=['POST'], detail=False, url_path='delete-many')
    def delete_many(self, request: Request):
        """
        Delete multiple SupplierUserAccess instances from the incoming payload.
        """
        with transaction.atomic():
            num_deleted = 0

            # loop over the accesses to delete
            for access_id in request.data.get('access_ids') or []:
                if self.access_policy.get_qs_for_user(request.user, action=AccessPolicyAction.DELETE).filter(id=access_id).exists():
                    user_access: SupplierUserAccess = SupplierUserAccess.objects.filter(id=access_id).first()
                    user_access.delete()
                    num_deleted += 1

        return Response({'num_deleted': num_deleted}, status=status.HTTP_200_OK)


class SupplierSuspendHistoryViewSet(AccessViewSetMixin, mixins.ListModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = SupplierSuspendHistorySerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = SupplierSuspendHistoryFilter
    ordering_fields = [f.name for f in SupplierSuspendHistory._meta.fields] + [
        'donor__name',
        'actor__email',
    ]
    access_policy = SupplierSuspendHistoryAccessPolicy

    def get_queryset(self) -> QuerySet[SupplierUserAccess]:
        return self.access_policy.scope_queryset(self.request)


class AssetViewSet(AccessViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin, viewsets.ModelViewSet):
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = AssetFilter
    ordering_fields = [f.name for f in Asset._meta.fields] + [
        'current_premises__place__name',
        'current_premises__place__admin_zone_2__admin_zone_1__country',
        'current_premises__place__acted_area__name',
        'current_premises__code',
        'current_project_contract__code_project',
        'current_project_contract__donor__name',
        'current_project_contract__donor_back__name',
        'current_logistics_budget_line__budget_line__code',
        'current_donor__name',
        'current_staff__email',
        'current_staff__full_name',
        'department__label_en',
        'department__label_fr',
        'country_mission__label_en',
        'country_mission__label_fr',
        'currency__code',
        'days_until_warranty_end',
    ]
    access_policy = AssetAccessPolicy
    lookup_field = 'code'

    def get_queryset(self) -> QuerySet[Asset]:
        return self.access_policy.scope_queryset(self.request)

    def get_serializer_class(self):
        return AssetListSerializer if self.action == 'list' else AssetDetailSerializer

    @action(methods=['GET'], detail=False, url_path='fetch-external-data')
    def fetch_external_data(self, request, *args, **kwargs):
        """
        Fetch various information from Intune and Lenovo websites for teh given serial number.
        WARNING: this end point is visible by anyone with `view_asset` permission,
                 make sure the returned data do not contain personal information or other sensitive data.
        """
        serial_number = request.query_params.get('serial_number', '')

        auth_token, _ = get_microsoft_graph_authentication_token()
        intune_data = get_intune_data(auth_token, find_intune_id(auth_token, serial_number))
        lenovo_data = None
        if intune_data is not None and 'lenovo' == intune_data.get('manufacturer', '').strip().lower():
            lenovo_data = get_lenovo_data(serial_number)
        if intune_data is not None:
            del intune_data['user']

        return Response(data={
            'intune': intune_data,
            'lenovo': lenovo_data,
        }, status=status.HTTP_200_OK)

    @action(methods=['GET'], detail=False, url_path='suggest-accessories')
    def suggest_accessories(self, request, *args, **kwargs):
        """
        Returns a set of "other accessories" matching the provided query.
        """
        limit = int(request.query_params.get('limit', '1000'))
        query = Asset.normalize_accessory_other(request.query_params.get('query', '')) or ''
        # Find "other accessories" on all Assets, not only the ones the user has access to.
        accessory_items = Asset.objects.filter(
            accessories_other__icontains=query,
        ).annotate(
            accessory_items=Func(F('accessories_other'), function='unnest'),
        ).values_list(
            'accessory_items', flat=True,
        ).order_by(
            'accessory_items',
        ).distinct()
        # Filter the list of "other accessories" for the ones containing the query, then limit the number of results if necessary.
        accessory_items = [i for i in accessory_items if query in i][:limit]
        return Response(data=accessory_items)

    @action(methods=['POST'], detail=False, url_path='export')
    def export(self, request: Request):
        # The export can be presented in en or fr.
        locale = get_language() or 'en'
        # The export can be filtered according to the current webapp filters.
        qs_filters = request.data.get('filters')

        # Create the long-running job with the locale and filters in its metadata.
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_ASSETS,
            detail={'locale': locale, 'filters': qs_filters},
        )
        # Launch the async task.
        export_assets.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)

    @action(methods=['POST'], detail=True, url_path='export-usages-pdf')
    def export_usages_pdf(self, request: Request, code=None):
        asset: Asset = self.get_object()

        # The export can be presented in en or fr.
        locale = get_language() or 'en'

        # Create the long-running job with the locale and filters in its metadata.
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_ASSET_USAGES,
            detail={'locale': locale, 'asset_code': asset.code},
        )
        # Launch the async task.
        export_asset_usages.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)


class AssetAllocationProjectContractViewSet(AccessViewSetMixin, AuditLogViewSetMixin, viewsets.ModelViewSet):
    serializer_class = AssetAllocationProjectContractSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = AssetAllocationProjectContractFilter
    ordering_fields = [f.name for f in AssetAllocationProjectContract._meta.fields] + [
        'project_contract__code_project',
        'logistics_budget_line__budget_line__code',
    ]
    access_policy = AssetAllocationProjectContractAccessPolicy

    def get_queryset(self) -> QuerySet[Asset]:
        return self.access_policy.scope_queryset(self.request)


class AssetAllocationPremisesViewSet(AccessViewSetMixin, AuditLogViewSetMixin, viewsets.ModelViewSet):
    serializer_class = AssetAllocationPremisesSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = AssetAllocationPremisesFilter
    ordering_fields = [f.name for f in AssetAllocationPremises._meta.fields] + [
        'premises__code',
    ]
    access_policy = AssetAllocationPremisesAccessPolicy

    def get_queryset(self) -> QuerySet[Asset]:
        return self.access_policy.scope_queryset(self.request)


class AssetAllocationUsageViewSet(AccessViewSetMixin, AuditLogViewSetMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                                  mixins.CreateModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    serializer_class = AssetAllocationUsagesSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = AssetAllocationUsagesFilter
    ordering_fields = [f.name for f in AssetAllocationUsage._meta.fields] + [
        'staff__code', 'staff__full_name',
    ]
    access_policy = AssetAllocationUsageAccessPolicy

    def get_queryset(self) -> QuerySet[Asset]:
        return self.access_policy.scope_queryset(self.request)


class ConfirmationViewSet(AccessViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin, AttachmentViewSetMixin,
                          mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = ConfirmationFilter
    ordering_fields = [f.name for f in Confirmation._meta.fields]
    access_policy = ConfirmationAccessPolicy
    lookup_field = 'code'

    def get_queryset(self) -> QuerySet[Asset]:
        return self.access_policy.scope_queryset(self.request)

    def get_serializer_class(self):
        return ConfirmationSerializer

    @action(methods=['POST'], detail=True, url_path='accept-with-upload')
    def accept_with_upload(self, request, code=None, *args, **kwargs):
        instance: Confirmation = self.get_object()

        files = self.request.FILES.getlist('theFile') or []
        for file in files:
            instance.attach(file.name, file.read(), created_by=request.user)

        Workflow.transition_to(instance, Confirmation.State.CONFIRMED, request.user)
        instance.refresh_from_db()

        serializer = ConfirmationSerializer(data=instance, context=self.get_serializer_context())
        serializer.is_valid()
        return Response(serializer.data, status=status.HTTP_200_OK)


class AssetUserAccessViewSet(AccessViewSetMixin, mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    serializer_class = AssetUserAccessSerializer
    access_policy = AssetUserAccessAccessPolicy
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    filterset_class = AssetUserAccessFilter
    ordering_fields = [f.name for f in AssetUserAccess._meta.fields] + [
        'country__label_en',
        'country__label_fr',
        'acted_area__name',
        'user__email',
    ]

    def get_queryset(self) -> QuerySet[AssetUserAccess]:
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='create-many')
    def create_many(self, request: Request):
        """
        Create multiple AssetUserAccess instances from the incoming payload,
        i.e. multiple ACTEDAreas for the given User.
        """
        with transaction.atomic():
            username = request.data.get('user', {}).get('username')
            mode = request.data.get('mode')
            country_code_iso = request.data.get('country', {}).get('code_iso')
            acted_areas = request.data.get('acted_areas') or []
            acted_area_codes = [area.get('code') for area in acted_areas] or [None]

            if not all([username, mode]):
                raise ValidationError({'__all__': ['missing fields']})

            user = User.objects.get(username=username)
            country = Country.objects.filter(code_iso=country_code_iso).first()

            for acted_area_code in acted_area_codes:
                acted_area = ACTEDArea.objects.filter(code=acted_area_code).first()

                can_create = self.access_policy.user_can_create_access(
                    request.user, country_code_iso, acted_area_code, mode,
                )

                if not can_create:
                    raise PermissionDenied()

                AssetUserAccess.objects.create(
                    user=user,
                    mode=mode,
                    country=country,
                    acted_area=acted_area,
                )

        return Response(status=status.HTTP_201_CREATED)

    @action(methods=['POST'], detail=False, url_path='delete-many')
    def delete_many(self, request: Request):
        """
        Delete multiple AssetUserAccess instances from the incoming payload.
        """
        with transaction.atomic():
            num_deleted = 0

            # loop over the accesses to delete
            for access_id in request.data.get('access_ids') or []:
                if self.access_policy.user_can_delete_access(request.user, access_id):
                    user_access: AssetUserAccess = AssetUserAccess.objects.filter(id=access_id).first()
                    user_access.delete()
                    num_deleted += 1

        return Response({'num_deleted': num_deleted}, status=status.HTTP_200_OK)


class InventoryViewSet(AccessViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin, mixins.ListModelMixin,
                       mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin,
                       mixins.CreateModelMixin, viewsets.GenericViewSet):
    lookup_field = 'code'
    access_policy = InventoryAccessPolicy
    filterset_class = InventoryFilter
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    ordering_fields = [f.name for f in Inventory._meta.fields] + [
        'premises__place__admin_zone_2__admin_zone_1__country__label_en',
        'premises__place__admin_zone_2__admin_zone_1__country__label_fr',
        'premises__place__name',
        'premises__code',
    ]

    def get_queryset(self) -> QuerySet[Inventory]:
        return self.access_policy.scope_queryset(self.request)

    def get_serializer_class(self):
        return InventoryListSerializer if self.action == 'list' else InventoryDetailSerializer

    @action(methods=['POST'], detail=True, url_path='export-pdf')
    def export_pdf(self, request: Request, code=None):
        inventory: Inventory = self.get_object()

        # The export can be presented in en or fr.
        locale = get_language() or 'en'

        # Create the long-running job with the locale and filters in its metadata.
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_ASSET_INVENTORY,
            detail={'locale': locale, 'inventory_code': inventory.code},
        )
        # Launch the async task.
        export_assets_inventory.delay(job.id)

        # return the job id to the client
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)

    @action(methods=['POST'], detail=False, url_path='export-inventories')
    def export_inventories(self, request):
        # Parse the request data
        export_type = request.data.get('type')
        start_date = request.data.get('start_date')
        end_date = request.data.get('end_date')
        project_contract_id = request.data.get('project_contract_id')

        # Validate the input
        if export_type == EXPORT_TYPE_PERIOD and not (start_date and end_date):
            return Response({'error': 'Missing start_date or end_date for period export.'}, status=400)
        if export_type == EXPORT_TYPE_PROJECT and not project_contract_id:
            return Response({'error': 'Missing project_contract_id for project export.'}, status=400)

        # Create the LongRunningJob
        job = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_ASSET_INVENTORY,
            detail={'type': export_type, 'start_date': start_date, 'end_date': end_date, 'current_project_contract_id': project_contract_id},
        )

        export_assets_inventories.delay(job.id)

        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)


class InventoryAssetRelationViewSet(AccessViewSetMixin, AuditLogViewSetMixin, mixins.DestroyModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = InventoryAssetRelationAccessPolicy
    serializer_class = InventoryAssetRelationSerializer
    filterset_class = InventoryAssetRelationFilter
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    ordering_fields = [f.name for f in InventoryAssetRelation._meta.fields]

    def get_queryset(self) -> QuerySet[InventoryAssetRelation]:
        return self.access_policy.scope_queryset(self.request)


class DisposalPlanViewSet(AccessViewSetMixin, AttachmentViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin,
                          mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                          mixins.DestroyModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    lookup_field = 'code'
    access_policy = DisposalPlanAccessPolicy
    filterset_class = DisposalPlanFilter
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    ordering_fields = [f.name for f in DisposalPlan._meta.fields] + [
        'donor__name',
        'project_contract__code_project',
    ]

    def get_queryset(self) -> QuerySet[DisposalPlan]:
        return self.access_policy.scope_queryset(self.request)

    def get_serializer_class(self):
        return DisposalPlanListSerializer if self.action == 'list' else DisposalPlanDetailSerializer

    @action(methods=['GET'], detail=True, url_path='make-donation-certificate')
    def make_donation_certificate(self, request: Request, code=None):
        """
        Download a pdf-rendered donation certificate template for this DisposalPlan.
        """
        disposal_plan: DisposalPlan = self.get_object()
        pdf_data = disposal_plan.make_donation_certificate()
        filename = f'donation-certificate-{disposal_plan.code}.pdf'
        disposition = 'attachment'

        response = HttpResponse(pdf_data, status=status.HTTP_200_OK)
        response['Content-Type'] = 'application/pdf'
        response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        response['Content-Disposition'] = "%s; filename=\"%s\"; filename*=UTF-8''%s" % (
            disposition,
            quote(filename, safe=''),
            quote(filename, safe=''),
        )
        return response


class AssetMaintenanceViewSet(AccessViewSetMixin, AttachmentViewSetMixin, WorkflowViewSetMixin, AuditLogViewSetMixin,
                              mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                              mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = AssetMaintenanceAccessPolicy
    filterset_class = AssetMaintenanceFilter
    serializer_class = AssetMaintenanceSerializer
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    ordering_fields = [f.name for f in AssetMaintenance._meta.fields] + [
        'asset__code',
        'currency__code',
        'supplier__name',
    ]

    def get_queryset(self) -> QuerySet[AssetMaintenance]:
        return self.access_policy.scope_queryset(self.request)


class PhoneLineVewSet(AccessViewSetMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    lookup_field = 'id'
    access_policy = PhoneLineAccessPolicy
    serializer_class = PhoneLineSerializer
    filterset_class = PhoneLineFilter
    filter_backends = (DjangoFilterBackend, filters.OrderingFilter)
    ordering_fields = [f.name for f in PhoneLine._meta.fields] + [
        'in_use',
    ]

    def get_queryset(self):
        return self.access_policy.scope_queryset(self.request)

    @action(methods=['POST'], detail=False, url_path='export')
    def export(self, request):
        # The export can be presented in en or fr.
        locale = get_language() or 'en'
        # The export can be filtered according to the current webapp filters.
        qs_filters = request.data.get('filters')

        # Create the long-running job with the filters in its metadata.
        job: LongRunningJob = LongRunningJob.objects.create(
            created_by=request.user,
            type=LongRunningJob.Type.EXPORT_PHONE_LINES,
            detail={'locale': locale, 'filters': qs_filters},
        )
        # Launch the async task.
        export_phone_lines.delay(job.id)

        # Return the job id to the client.
        data = LongRunningJobSerializer(job).data
        return Response(data, status=status.HTTP_200_OK)
