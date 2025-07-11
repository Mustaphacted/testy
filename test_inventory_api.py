import os
import tempfile
import zipfile
from datetime import date
from unittest import mock

import pytest
from django.utils import translation

from core.models.models import LongRunningJob, Role
from core.tests.utils import create_user
from grants_management.tests.utils import create_project_contract
from logistics.models.assets import Asset, Inventory, InventoryAssetRelation
from logistics.tasks.assets_inventory_export import (
    get_assets_by_period, get_assets_by_project, create_zip_with_inventories, 
    export_inventory, export_assets_inventories
)
from logistics.tests.utils_assets import create_asset, create_inventory, create_asset_allocation_premises
from security.tests.utils_premises import create_premises


@pytest.mark.django_db
def test_get_assets_by_period(country_fixtures):
    """Test get_assets_by_period function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    
    # Create inventory with end date
    inventory = create_inventory(premises=premises, date_end=date(2022, 6, 30))
    
    # Test with date objects
    assets = get_assets_by_period(date(2022, 1, 1), date(2022, 12, 31))
    assert asset in assets
    
    # Test with string dates
    assets = get_assets_by_period('2022-01-01', '2022-12-31')
    assert asset in assets
    
    # Test with no inventories in period
    assets = get_assets_by_period(date(2023, 1, 1), date(2023, 12, 31))
    assert not assets.exists()


@pytest.mark.django_db
def test_get_assets_by_project(country_fixtures):
    """Test get_assets_by_project function."""
    project_contract = create_project_contract()
    asset = create_asset()
    
    # Set current project for asset
    asset.current_project_contract = project_contract
    asset.save()
    
    # Test getting currently allocated assets
    assets = get_assets_by_project(project_contract.id, include_historical=False)
    assert asset in assets
    
    # Test with non-existent project
    assets = get_assets_by_project(99999, include_historical=False)
    assert not assets.exists()


@pytest.mark.django_db
def test_create_zip_with_inventories(country_fixtures):
    """Test create_zip_with_inventories function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    
    # Create inventory
    inventory = create_inventory(premises=premises)
    
    # Get assets queryset
    assets = Asset.objects.filter(id=asset.id)
    
    with translation.override('en'):
        zip_path = create_zip_with_inventories(assets)
        
        assert os.path.exists(zip_path)
        assert os.path.getsize(zip_path) > 0
        
        # Check zip contents
        with zipfile.ZipFile(zip_path, 'r') as zip_file:
            file_list = zip_file.namelist()
            assert len(file_list) >= 1
            assert any(file.endswith('.pdf') for file in file_list)
        
        os.remove(zip_path)


@pytest.mark.django_db
def test_export_inventory(country_fixtures):
    """Test export_inventory function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    project_contract = create_project_contract()
    
    # Set current project for asset
    asset.current_project_contract = project_contract
    asset.save()
    
    # Create inventory
    inventory = create_inventory(premises=premises, date_end=date(2022, 6, 30))
    
    with translation.override('en'):
        # Test period export
        zip_path = export_inventory(
            export_type='period',
            date_start=date(2022, 1, 1),
            date_end=date(2022, 12, 31),
        )
        assert os.path.exists(zip_path)
        os.remove(zip_path)
        
        # Test project export
        zip_path = export_inventory(
            export_type='project',
            project_contract_id=project_contract.id,
        )
        assert os.path.exists(zip_path)
        os.remove(zip_path)
        
        # Test invalid export type
        with pytest.raises(ValueError, match='Invalid export type'):
            export_inventory(export_type='invalid')


@pytest.mark.django_db
def test_export_assets_inventories(country_fixtures):
    """Test export_assets_inventories celery task."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    
    # Create inventory
    inventory = create_inventory(premises=premises, date_end=date(2022, 6, 30))
    
    # Create job
    job = LongRunningJob.objects.create(
        created_by=create_user([Role.Name.ASSETS_ADMIN]),
        type=LongRunningJob.Type.EXPORT_ASSET_INVENTORY,
        detail={
            'type': 'period',
            'start_date': '2022-01-01',
            'end_date': '2022-12-31',
            'locale': 'en'
        }
    )
    
    # Test the task
    export_assets_inventories(job.id)
    
    job.refresh_from_db()
    assert job.status == LongRunningJob.Status.DONE
    assert job.attachments.count() == 1