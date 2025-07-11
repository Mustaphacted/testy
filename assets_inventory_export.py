import logging
import zipfile
import os
import tempfile

from datetime import date
from django.utils.dateparse import parse_date
from typing import Dict

from django.utils import translation
from django.db.models import QuerySet
from django.db.models.query import Prefetch
from logistics.models.assets import InventoryAssetRelation


from acted_ims.celery import app
from core.middleware.current_user import get_current_user
from core.models.models import LongRunningJob
from core.pdf import render_template, make_from_html
from logistics.models.assets import Inventory, Asset

logger = logging.getLogger(__name__)


@app.task
def export_assets_inventory(job_id: int) -> None:
    job = LongRunningJob.objects.get(id=job_id)
    detail: Dict[str, str] = job.detail
    translation.activate(detail.get('locale') or 'en')

    try:
        inventory = Inventory.objects.get(code=detail['inventory_code'])

        pdf_as_bytes = _inventory_to_pdf(inventory)
        attachment = job.attach('inventory.pdf', pdf_as_bytes, created_by=get_current_user())
        job.status = job.Status.DONE
        job.progress = 100
        job.message = {'attach_uuid': str(attachment.uuid)}
        job.save()
    except Exception as e:
        logger.exception('error processing task')
        job.status = LongRunningJob.Status.ERROR
        job.progress = 100
        job.message = {'message': str(e)}
        job.save()

    translation.deactivate()


def _inventory_to_pdf(inventory: Inventory) -> bytes:
    locale = translation.get_language()

    # use filtered relations if it exist
    relations = getattr(inventory, 'filtered_relations', None) or inventory.inventory_asset_relations.all()

    html = render_template(
        f'logistics/pdf_templates/inventory-{locale}.html',
        inventory=inventory,
        inventory_asset_relations=relations,
        date=date.today(),
    )
    return make_from_html(html)


def get_assets_by_period(date_start, date_end) -> QuerySet:
    """
    Get assets from inventories that ended within the specified date range.
    """
    # Convert string dates to date objects if needed
    if isinstance(date_start, str):
        date_start = parse_date(date_start)
    if isinstance(date_end, str):
        date_end = parse_date(date_end)

    # Get inventories with end date in the range
    inventories_with_end = Inventory.objects.filter(
        date_end__isnull=False,
        date_end__gte=date_start,
        date_end__lte=date_end,
    )

    # If no completed inventories found, return empty queryset
    if not inventories_with_end.exists():
        return Asset.objects.none()

    # Get all assets linked to these inventories
    assets = Asset.objects.filter(
        inventory_asset_relations__inventory__in=inventories_with_end,
    ).distinct()

    return assets


def get_assets_by_project(project_contract_id: int, include_historical: bool = True) -> QuerySet:
    """
    Get assets allocated to a specific project.

    Returns:
        QuerySet of Asset objects associated with the project
    """
    if include_historical:
        # Get all assets that have ever been allocated to this project
        from logistics.models.assets import AssetAllocationProjectContract
        asset_ids = AssetAllocationProjectContract.objects.filter(
            project_contract_id=project_contract_id,
        ).values_list('asset_id', flat=True).distinct()
        return Asset.objects.filter(id__in=asset_ids)
    else:
        # Get only assets currently allocated to this project
        return Asset.objects.filter(current_project_contract_id=project_contract_id)


def create_zip_with_inventories(assets: QuerySet, project_contract_id=None, output_path=None) -> str:
    pdf_files = []
    processed_inventory_ids = set()

    asset_relations_query = InventoryAssetRelation.objects.filter(asset__in=assets)
    if project_contract_id:
        asset_relations_query = asset_relations_query.filter(asset__current_project_contract_id=project_contract_id)

    inventories = Inventory.objects.filter(
        inventory_asset_relations__asset__in=assets,
    ).prefetch_related(
        Prefetch(
            'inventory_asset_relations',
            queryset=asset_relations_query,
            to_attr='filtered_relations',
        ),
    ).distinct()

    if not inventories:
        raise Exception('No inventories found for the selected criteria.')

    for inventory in inventories:
        if inventory.id in processed_inventory_ids:
            continue

        processed_inventory_ids.add(inventory.id)
        pdf_data = _inventory_to_pdf(inventory)

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as temp_file:
            temp_file.write(pdf_data)
            temp_file.flush()
            pdf_files.append(temp_file.name)

    if not pdf_files:
        raise Exception('No PDF inventory to export for selected criteria.')

    # Use provided output_path or create a temporary file
    if output_path is None:
        output_path = tempfile.NamedTemporaryFile(suffix='.zip', delete=False).name

    with zipfile.ZipFile(output_path, 'w') as zip_file:
        for pdf_file in pdf_files:
            zip_file.write(pdf_file, os.path.basename(pdf_file))

    for pdf_file in pdf_files:
        os.remove(pdf_file)

    return output_path


def export_inventory(export_type, date_start=None, date_end=None, project_contract_id=None) -> str:
    """
    Export inventory data based on the specified criteria.

    Args:
        export_type: Type of export ('period' or 'project')
        date_start: Start date for period export
        date_end: End date for period export
        project_contract_id: Project contract ID for project export

    Returns:
        Path to the generated ZIP file
    """
    if export_type == 'period':
        assets = get_assets_by_period(date_start, date_end)
        if project_contract_id:
            assets = assets.filter(current_project_contract_id=project_contract_id)
    elif export_type == 'project':
        assets = get_assets_by_project(project_contract_id)
    else:
        raise ValueError("Invalid export type: choose either 'period' or 'project'.")

    return create_zip_with_inventories(assets, project_contract_id=project_contract_id)


@app.task
def export_assets_inventories(job_id: int) -> None:
    """
       Asynchronous task to export asset inventories to a ZIP file.

       This function processes a long-running job to generate an inventory export based on
       different criteria (period-based or project-based). The export is packaged as a ZIP
       file and attached to the job for download.

       The job detail should contain:
           - type: Export type ('period' or 'project')
           - start_date/date_start: Start date for period exports
           - end_date/date_end: End date for period exports
           - current_project_contract_id: Project contract ID for project exports
           - locale: Language locale for the export (defaults to 'en')

       The function ensures proper cleanup of temporary files and deactivates
       translation settings regardless of success or failure.
       """
    job = LongRunningJob.objects.get(id=job_id)
    locale = job.detail.get('locale', 'en')
    translation.activate(locale)

    try:
        export_type = job.detail.get('type')
        date_start = job.detail.get('start_date') or job.detail.get('date_start')
        date_end = job.detail.get('end_date') or job.detail.get('date_end')
        project_contract_id = job.detail.get('current_project_contract_id')

        zip_path = export_inventory(
            export_type=export_type,
            date_start=date_start,
            date_end=date_end,
            project_contract_id=project_contract_id,
        )

        with open(zip_path, 'rb') as temp_file:
            attachment = job.attach(
                'inventories_export.zip', temp_file.read(), created_by=job.created_by,
            )

        os.remove(zip_path)

        job.status = LongRunningJob.Status.DONE
        job.progress = 100
        job.message = {'attach_uuid': str(attachment.uuid)}

    except Exception as e:
        logger.exception('Error processing export_inventory_task')
        job.status = LongRunningJob.Status.ERROR
        job.progress = 100
        job.message = {'message': str(e)}

    finally:
        job.save()
        translation.deactivate()
