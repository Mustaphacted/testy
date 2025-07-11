import json
import logging
import os
import zipfile
from datetime import date
from unittest import mock
from unittest.mock import Mock

import pytest
from django.test import Client
from django.utils import translation
from freezegun import freeze_time
from pypdf import PdfReader
from rest_framework import status

from grants_management.tests.utils import create_project_contract
from core.models.models import Role, LongRunningJob
from core.tests.utils import create_user
from hr.tests.utils_staff import create_staff
from logistics.models.assets import Asset, InventoryAssetRelation
from logistics.tasks.assets_inventory_export import (
    export_assets_inventory, get_assets_by_period, get_assets_by_project, export_inventory
)
from logistics.tests.utils_assets import (
    create_asset_allocation_premises, create_asset, create_inventory, create_asset_in_state_with_usage,
)
from security.models import PremisesRoom
from security.tests.utils_premises import create_premises

logger = logging.getLogger(__name__)


@pytest.mark.django_db
def test_export_assets_inventory():
    """Test the export_assets_inventory function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    inventory = create_inventory(premises=premises)
    
    # Create a job
    job = LongRunningJob.objects.create(
        created_by=create_user([Role.Name.ASSETS_ADMIN]),
        type=LongRunningJob.Type.EXPORT_ASSET_INVENTORY,
        detail={'locale': 'en', 'inventory_code': inventory.code}
    )
    
    # Test the function
    with translation.override('en'):
        export_assets_inventory(job.id)
    
    job.refresh_from_db()
    assert job.status == LongRunningJob.Status.DONE
    assert job.attachments.count() == 1


@pytest.mark.django_db
def test_get_assets_by_period():
    """Test the get_assets_by_period function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    
    # Create inventory with end date
    inventory = create_inventory(
        premises=premises,
        date_start=date(2022, 1, 1),
        date_end=date(2022, 6, 30),
    )
    
    # Test the function
    assets = get_assets_by_period(date(2022, 1, 1), date(2022, 12, 31))
    assert asset in assets
    
    # Test with no inventories in period
    assets = get_assets_by_period(date(2023, 1, 1), date(2023, 12, 31))
    assert not assets.exists()


@pytest.mark.django_db
def test_get_assets_by_project():
    """Test the get_assets_by_project function."""
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
def test_export_inventory():
    """Test the export_inventory function."""
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)
    project_contract = create_project_contract()
    
    # Set current project for asset
    asset.current_project_contract = project_contract
    asset.current_premises = premises
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