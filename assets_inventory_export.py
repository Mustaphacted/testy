import logging
import zipfile
import os
from datetime import date

from django.utils import translation
from django.db.models import QuerySet
from django.db.models.query import Prefetch
from django.utils.translation.trans_null import deactivate
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
    translation.activate(job.detail.get('locale') or 'en')

    try:
        inventory = Inventory.objects.get(code=job.detail['inventory_code'])

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


def get_assets_by_period(start_date, end_date) -> QuerySet:
    from django.utils.dateparse import parse_date
    from django.core.exceptions import ValidationError

    # Convert string dates to date objects if needed
    if isinstance(start_date, str):
        start_date = parse_date(start_date)
    if isinstance(end_date, str):
        end_date = parse_date(end_date)

    # Get inventories with end date in the range
    inventories_with_end = Inventory.objects.filter(
        date_end__isnull=False,
        date_end__gte=start_date,
        date_end__lte=end_date,
    )

    # If we don't find any inventories with end date, check if there are only inventories without end date
    if not inventories_with_end.exists():
        inventories_without_end = Inventory.objects.filter(
            date_start__lte=end_date,
            date_end__isnull=True,
        )

        if inventories_without_end.exists():
            raise ValidationError('Only inventories in progress in the specified period')
        return Asset.objects.none()

    # Get all assets linked to these inventories
    assets = Asset.objects.filter(
        inventory_asset_relations__inventory__in=inventories_with_end,
    ).distinct()

    return assets


def get_assets_by_project(project_contract_id: int) -> QuerySet:
    return Asset.objects.filter(current_project_contract_id=project_contract_id)


def create_zip_with_inventories(assets: QuerySet, project_contract_id=None) -> str:
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

    for i, inventory in enumerate(inventories):
        if inventory.id in processed_inventory_ids:
            continue

        processed_inventory_ids.add(inventory.id)

        pdf_data = _inventory_to_pdf(inventory)
        pdf_path = f'/tmp/inventory_{inventory.code}.pdf'
        with open(pdf_path, 'wb') as f:
            f.write(pdf_data)
        pdf_files.append(pdf_path)

    if not pdf_files:
        raise Exception('No PDF inventory to export for selected criteria.')

    zip_filename = '/tmp/inventories_export.zip'

    with zipfile.ZipFile(zip_filename, 'w') as zip_file:
        for pdf_file in pdf_files:
            zip_file.write(pdf_file, os.path.basename(pdf_file))

    for pdf_file in pdf_files:
        os.remove(pdf_file)

    return zip_filename


def export_inventory(export_type, start_date=None, end_date=None, project_contract_id=None) -> str:
    if export_type == 'period':
        assets = get_assets_by_period(start_date, end_date)
        if project_contract_id:
            assets = assets.filter(current_project_contract_id=project_contract_id)
    elif export_type == 'project':
        assets = get_assets_by_project(project_contract_id)
    else:
        raise ValueError("Invalid export type: choose either 'period' or 'project'.")

    return create_zip_with_inventories(assets)


@app.task
def export_assets_inventories(job_id: int) -> None:
    # Retrieve the LongRunningJob instance from the database
    job = LongRunningJob.objects.get(id=job_id)
    locale = job.detail.get('locale', 'en')
    translation.activate(locale)

    try:
        export_type = job.detail.get('type')
        start_date = job.detail.get('start_date')
        end_date = job.detail.get('end_date')
        project_contract_id = job.detail.get('current_project_contract_id')

        zip_file_path = export_inventory(
            export_type=export_type,
            start_date=start_date,
            end_date=end_date,
            project_contract_id=project_contract_id,
        )

        # Attach the ZIP file to the job
        with open(zip_file_path, 'rb') as zip_file:
            attachment = job.attach(
                'inventories_export.zip', zip_file.read(), created_by=get_current_user(),
            )

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
        deactivate()
