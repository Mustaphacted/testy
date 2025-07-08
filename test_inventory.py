import logging
from datetime import date
from unittest import mock
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.test import override_settings
from freezegun import freeze_time

from core.models.models import ACTEDArea, Role
from core.tests.utils import create_user
from core.tests.utils_admin_units import create_test_zones
from logistics.models.assets import Asset, Inventory, InventoryAssetRelation
from logistics.tests.utils_assets import create_asset, create_asset_allocation_premises, create_inventory
from security.models import PremisesRoom
from security.tests.utils_premises import create_premises
from workflows.models import Workflow

logger = logging.getLogger(__name__)


@pytest.mark.django_db
def test_inventory_required_fields(country_fixtures):
    with pytest.raises(ValidationError) as exception_information:
        Inventory.objects.create()
    errors = exception_information.value.message_dict
    assert len(errors) == 1
    assert errors['premises'] == ['This field cannot be null.']

    # Some fields are required in state VALIDATED
    inventory = Inventory.objects.create(premises=create_premises())
    with pytest.raises(ValidationError) as exception_information:
        inventory.state = Inventory.State.VALIDATED
        inventory.save()
    errors = exception_information.value.message_dict
    assert len(errors) == 1
    assert errors['date_end'] == ['This field is required.']


@pytest.mark.django_db
@freeze_time('2020-01-02 00:00:00')
def test_inventory_create(country_fixtures):
    """
    When an Inventory is created, it is automatically linked to all Assets in given Premises.
    """
    country1, country2 = create_test_zones()
    country1.code_acted = '91'
    country1.save()
    country2.code_acted = '92'
    country2.save()
    acted_area1 = ACTEDArea.objects.create(country=country1, mission_country=country1, name='acted_area1')
    asset1 = create_asset(country_mission=country1)
    asset2 = create_asset(country_mission=country1)
    asset3 = create_asset(country_mission=country1)
    premises1 = create_premises(country_code=country1.code_iso, acted_area=acted_area1)
    premises2 = create_premises(country_code=country1.code_iso, acted_area=acted_area1)
    create_asset_allocation_premises(asset=asset1, premises=premises1)
    create_asset_allocation_premises(asset=asset2, premises=premises1)
    create_asset_allocation_premises(asset=asset3, premises=premises2)
    room11 = PremisesRoom.objects.create(premises=premises1, name='room11')
    PremisesRoom.objects.create(premises=premises1, name='room12')
    PremisesRoom.objects.create(premises=premises2, name='room21')
    asset1.refresh_from_db()
    asset2.refresh_from_db()

    # Inventory is created with default values and Assets in Premises.
    inventory = Inventory.objects.create(premises=premises1)
    assert inventory.code is not None
    assert inventory.state == Inventory.State.ON_GOING
    assert inventory.premises == premises1
    assert inventory.date_start == date(2020, 1, 2)
    assert inventory.date_end is None
    assert list(inventory.assets.order_by('id')) == [asset1, asset2]

    # InventoryAssetRelation is created with condition from Asset.
    inventory_asset_relation1: InventoryAssetRelation = InventoryAssetRelation.objects.filter(inventory=inventory).order_by('id').first()
    assert inventory_asset_relation1.condition == asset1.condition
    assert inventory_asset_relation1.room is None
    assert inventory_asset_relation1.presence is None

    # When creating a new inventory, asset room is pre-filled with room of previous inventory.
    inventory_asset_relation1.room = room11
    inventory_asset_relation1.presence = Asset.Presence.PRESENT
    inventory_asset_relation1.save()
    inventory.date_end = date(2020, 1, 3)
    inventory.state = Inventory.State.VALIDATED
    inventory.save()
    Inventory.objects.create(premises=premises1, date_start=date(2020, 1, 4))
    inventory_asset_relation2: InventoryAssetRelation = InventoryAssetRelation.objects.filter(inventory=inventory).order_by('id').first()
    assert inventory_asset_relation2.room == room11

    # Can't create an inventory if a pending one already exists.
    with pytest.raises(ValidationError) as exception_information:
        Inventory.objects.create(premises=premises1)
    errors = exception_information.value.message_dict
    assert len(errors) == 1
    assert errors['premises'] == ['There is already an on-going inventory for the selected premises.']


@override_settings(ACTED_FRONT_URL='https://test.acted.dev/')
@pytest.mark.django_db
def test_get_url(country_fixtures):
    inventory = create_inventory(code='XXXXXX')

    # get_url returns URL to the Premises in given locale (defaults to 'en')
    assert inventory.get_url() == 'https://test.acted.dev/en/logistics/inventories/detail/XXXXXX'
    assert inventory.get_url('xx') == 'https://test.acted.dev/xx/logistics/inventories/detail/XXXXXX'


@pytest.mark.django_db
@mock.patch('core.tasks.send_mail.send_async_mail.delay')
def test_inventory_validation_updates_assets(mock_send_mail: Mock, country_fixtures):
    """
    When an inventory is validated:
    - asset condition is updated accordingly
    - asset being transferred are transferred
    """
    asset1 = create_asset()
    premises1 = create_premises()
    create_asset_allocation_premises(asset=asset1, premises=premises1)
    assert asset1.condition == Asset.Condition.NEW

    # Create an inventory with existing asset and add transferring assets (one already allocation to Premises and one not allocated).
    inventory = create_inventory(premises=premises1, state=Inventory.State.PENDING_VALIDATION)
    asset2 = create_asset()
    premises2 = create_premises()
    create_asset_allocation_premises(asset=asset2, premises=premises2)
    relation2 = InventoryAssetRelation.objects.create(inventory=inventory, room=premises1.rooms.first(), asset=asset2, condition=Asset.Condition.GOOD, transferring=True)
    asset3 = create_asset()
    relation3 = InventoryAssetRelation.objects.create(inventory=inventory, room=premises1.rooms.first(), asset=asset3, condition=Asset.Condition.GOOD, transferring=True)

    # Validated inventory.
    validating_user = create_user([Role.Name.ASSETS_MACHINE])
    Workflow.transition_to(inventory, Inventory.State.VALIDATED, validating_user)
    inventory.refresh_from_db()
    # inventory date_end was populated
    assert inventory.date_end
    # `validated_by` and `validated_at` were updated
    assert inventory.validated_by == validating_user
    assert inventory.validated_at

    # Asset's condition has been updated.
    asset1.refresh_from_db()
    asset2.refresh_from_db()
    asset3.refresh_from_db()
    relation2.refresh_from_db()
    relation3.refresh_from_db()
    assert asset1.condition == Asset.Condition.MEDIUM
    assert asset2.condition == Asset.Condition.GOOD
    # Transferred assets has been transferred.
    assert asset2.current_premises == premises1
    assert relation2.transferred_from == premises2
    assert asset3.current_premises == premises1
    assert relation3.transferred_from is None
