import json
import logging
import os
import zipfile
from datetime import date
from unittest import mock
from unittest.mock import Mock

import pytest
from django.test import Client
from freezegun import freeze_time
from pypdf import PdfReader
from rest_framework import status

from grants_management.tests.utils import create_project_contract

from core.models.models import Role, LongRunningJob
from core.tests.utils import create_user
from hr.tests.utils_staff import create_staff
from logistics.models.assets import Asset, InventoryAssetRelation
from logistics.tasks.assets_inventory_export import export_assets_inventory
from logistics.tests.utils_assets import (
    create_asset_allocation_premises, create_asset, create_inventory, create_asset_in_state_with_usage,
)
from security.models import PremisesRoom
from security.tests.utils_premises import create_premises

logger = logging.getLogger(__name__)


@pytest.mark.django_db
@freeze_time('2020-01-02 00:00:00')
def test_inventory_create(country_fixtures, client: Client):
    asset = create_asset()
    premises = create_premises()
    create_asset_allocation_premises(asset=asset, premises=premises)
    asset.refresh_from_db()

    payload = {
        'premises': {'id': premises.id},
    }

    url = '/api/logistics/inventories/'

    # anonymous user can't create
    response = client.post(url, data=json.dumps(payload), content_type='application/json')
    assert response.status_code == status.HTTP_401_UNAUTHORIZED

    # neither can viewer user
    client.force_login(user=create_user([Role.Name.CORE_VIEWER]))
    response = client.post(url, data=json.dumps(payload), content_type='application/json')
    assert response.status_code == status.HTTP_403_FORBIDDEN

    # assets:admin can create
    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    response = client.post(url, data=json.dumps(payload), content_type='application/json')
    assert response.status_code == status.HTTP_201_CREATED

    assert response.json()['id'] is not None
    assert response.json()['code'] is not None
    assert response.json()['date_start'] == '2020-01-02'
    assert response.json()['premises']['id'] == premises.id
    assert response.json()['premises']['code'] == premises.code
    assert response.json()['premises']['mission_country']['code_iso'] == premises.mission_country.code_iso
    assert response.json()['premises']['place']['name'] == premises.place.name
    assert response.json()['assets'][0]['room'] is None
    assert response.json()['assets'][0]['presence'] is None
    assert response.json()['assets'][0]['condition'] == asset.condition
    assert response.json()['assets'][0]['comments'] is None
    assert response.json()['assets'][0]['transferring'] is False
    assert response.json()['assets'][0]['asset']['code'] == asset.code
    assert response.json()['assets'][0]['asset']['category'] == asset.category
    assert response.json()['assets'][0]['asset']['sub_category'] == asset.sub_category
    assert response.json()['assets'][0]['asset']['type'] == asset.type
    assert response.json()['assets'][0]['asset']['current_staff'] is None
    assert response.json()['assets'][0]['asset']['current_premises']['code'] == asset.current_premises.code
    assert response.json()['assets'][0]['asset']['current_premises']['place']['name'] == asset.current_premises.place.name


@pytest.mark.django_db
@freeze_time('2020-01-02 00:00:00')
def test_inventory_retrieve(country_fixtures, client: Client, django_assert_max_num_queries):
    premises = create_premises()
    for n in range(50):
        create_asset_allocation_premises(asset=create_asset(), premises=premises)
    inventory = create_inventory(premises=premises)
    relation: InventoryAssetRelation = inventory.inventory_asset_relations.first()

    url = f'/api/logistics/inventories/{inventory.code}/'

    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    with django_assert_max_num_queries(30):  # Should not do a query per asset, so must be < 50.
        response = client.get(url, content_type='application/json')
        assert response.status_code == status.HTTP_200_OK

        assert response.json()['id'] is not None
        assert response.json()['code'] is not None
        assert response.json()['date_start'] == '2020-01-02'
        assert response.json()['premises']['id'] == premises.id
        assert response.json()['premises']['code'] == premises.code
        assert response.json()['premises']['mission_country']['code_iso'] == premises.mission_country.code_iso
        assert response.json()['premises']['place']['name'] == premises.place.name
        assert len(response.json()['assets']) == 50
        response_relation = [a for a in response.json()['assets'] if a['id'] == relation.id][0]
        assert response_relation['room'] is None
        assert response_relation['presence'] is None
        assert response_relation['condition'] == relation.asset.condition
        assert response_relation['comments'] is None
        assert response_relation['transferring'] is False
        assert response_relation['asset']['code'] == relation.asset.code
        assert response_relation['asset']['category'] == relation.asset.category
        assert response_relation['asset']['sub_category'] == relation.asset.sub_category
        assert response_relation['asset']['type'] == relation.asset.type
        assert response_relation['asset']['current_staff'] is None
        assert response_relation['asset']['current_premises']['code'] == relation.asset.current_premises.code
        assert response_relation['asset']['current_premises']['place']['name'] == relation.asset.current_premises.place.name


@pytest.mark.django_db
@freeze_time('2020-01-02 00:00:00')
def test_inventory_list(country_fixtures, client: Client):
    asset = create_asset()
    premises = create_premises()
    create_asset_allocation_premises(asset=asset, premises=premises)
    asset.refresh_from_db()
    inventory = create_inventory(premises=premises)

    url = '/api/logistics/inventories/'

    # anonymous user can't list or view
    assert client.get(url).status_code == status.HTTP_401_UNAUTHORIZED

    # neither can viewer user
    client.force_login(user=create_user([Role.Name.CORE_VIEWER]))
    assert client.get(url).status_code == status.HTTP_403_FORBIDDEN

    # assets:admin can list
    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK

    assert response.json()['count'] == 1
    assert response.json()['results'][0]['id'] == inventory.id
    assert response.json()['results'][0]['code'] == inventory.code
    assert response.json()['results'][0]['date_start'] == '2020-01-02'
    assert response.json()['results'][0]['premises']['id'] == premises.id
    assert response.json()['results'][0]['premises']['code'] == premises.code
    assert response.json()['results'][0]['premises']['mission_country']['code_iso'] == premises.mission_country.code_iso
    assert response.json()['results'][0]['premises']['place']['name'] == premises.place.name
    assert 'assets' not in response.json()['results'][0]


@pytest.mark.django_db
@freeze_time('2020-01-02 00:00:00')
@mock.patch('core.tasks.send_mail.send_async_mail.delay')
def test_inventory_update(mock_send_mail: Mock, country_fixtures, client: Client):
    staff = create_staff()
    asset = create_asset_in_state_with_usage(target_state=Asset.State.IN_USE, staff=staff)
    premises = create_premises()
    room = PremisesRoom.objects.create(premises=premises, name='Room')
    create_asset_allocation_premises(asset=asset, premises=premises)
    asset.refresh_from_db()
    inventory = create_inventory(premises=premises)

    url = f'/api/logistics/inventories/{inventory.code}/'

    payload = {
        'premises': {'id': 'invalid'},
        'date_start': '2024-01-02',
        'assets': [
            {
                'id': InventoryAssetRelation.objects.get(inventory=inventory).id,
                'condition': Asset.Condition.BAD,
                'presence': Asset.Presence.PRESENT,
                'comments': 'a comment',
                'room': {'id': room.id},
            },
        ],
    }

    # anonymous user can't update
    assert client.patch(url, data=json.dumps(payload), content_type='application/json').status_code == status.HTTP_401_UNAUTHORIZED

    # neither can viewer user
    client.force_login(user=create_user([Role.Name.CORE_VIEWER]))
    assert client.patch(url, data=json.dumps(payload), content_type='application/json').status_code == status.HTTP_403_FORBIDDEN

    # assets:admin can update
    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    response = client.patch(url, data=json.dumps(payload), content_type='application/json')
    assert response.status_code == status.HTTP_200_OK
    assert response.json()['id'] == inventory.id
    assert response.json()['code'] == inventory.code
    assert response.json()['date_start'] == '2024-01-02'
    assert response.json()['premises']['id'] == premises.id
    assert response.json()['premises']['code'] == premises.code
    assert response.json()['premises']['mission_country']['code_iso'] == premises.mission_country.code_iso
    assert response.json()['premises']['place']['name'] == premises.place.name
    assert response.json()['assets'][0]['room']['id'] == room.id
    assert response.json()['assets'][0]['room']['name'] == room.name
    assert response.json()['assets'][0]['presence'] == Asset.Presence.PRESENT
    assert response.json()['assets'][0]['condition'] == Asset.Condition.BAD
    assert response.json()['assets'][0]['comments'] == 'a comment'
    assert response.json()['assets'][0]['transferring'] is False
    assert response.json()['assets'][0]['asset']['code'] == asset.code
    assert response.json()['assets'][0]['asset']['category'] == asset.category
    assert response.json()['assets'][0]['asset']['sub_category'] == asset.sub_category
    assert response.json()['assets'][0]['asset']['type'] == asset.type
    assert response.json()['assets'][0]['asset']['current_staff']['email'] == staff.email
    assert response.json()['assets'][0]['asset']['current_staff']['given_name'] == staff.given_name
    assert response.json()['assets'][0]['asset']['current_staff']['surname'] == staff.surname
    assert response.json()['assets'][0]['asset']['current_premises']['code'] == asset.current_premises.code
    assert response.json()['assets'][0]['asset']['current_premises']['place']['name'] == asset.current_premises.place.name


@pytest.mark.django_db
def test_inventory_delete(country_fixtures, client: Client):
    staff = create_staff()
    asset = create_asset_in_state_with_usage(target_state=Asset.State.IN_USE, staff=staff)
    premises = create_premises()
    create_asset_allocation_premises(asset=asset, premises=premises)
    asset.refresh_from_db()
    inventory = create_inventory(premises=premises)
    assert InventoryAssetRelation.objects.count() == 1

    url = f'/api/logistics/inventories/{inventory.code}/'

    # anonymous user can't update
    assert client.delete(url).status_code == status.HTTP_401_UNAUTHORIZED

    # neither can viewer user
    client.force_login(user=create_user([Role.Name.CORE_VIEWER]))
    assert client.delete(url).status_code == status.HTTP_403_FORBIDDEN

    # assets:admin can update
    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    response = client.delete(url)
    assert response.status_code == status.HTTP_204_NO_CONTENT
    # InventoryAssetRelation objects have been deleted too.
    assert InventoryAssetRelation.objects.count() == 0


@mock.patch('logistics.views.export_assets_inventory.delay')
@pytest.mark.django_db
def test_inventory_export_pdf(mock_export: Mock, country_fixtures, client: Client):
    staff = create_staff()
    asset = create_asset_in_state_with_usage(target_state=Asset.State.IN_USE, staff=staff)
    premises = create_premises()
    create_asset_allocation_premises(asset=asset, premises=premises)
    asset.refresh_from_db()
    inventory = create_inventory(premises=premises)
    assert InventoryAssetRelation.objects.count() == 1

    url = f'/api/logistics/inventories/{inventory.code}/export-pdf/'

    # anonymous user can't export
    assert client.post(url).status_code == status.HTTP_401_UNAUTHORIZED

    # neither can viewer user
    assert client.post(url).status_code == status.HTTP_401_UNAUTHORIZED

    # assets:admin can export
    client.force_login(user=create_user([Role.Name.ASSETS_ADMIN]))
    response = client.post(url)
    assert response.status_code == status.HTTP_200_OK

    job = LongRunningJob.objects.get(code=response.json()['code'])

    assert LongRunningJob.objects.get(code=response.json()['code'])
    assert job.status == LongRunningJob.Status.WAITING
    assert job.type == LongRunningJob.Type.EXPORT_ASSET_INVENTORY

    # no attachment yet
    assert job.attachments.count() == 0
    # verify the celery delay() function was called with the right input,
    mock_export.assert_called_once_with(job.id)

    # now call the same function synchronously, simulating the celery call / worker
    export_assets_inventory(job.id)
    # there should now be an attachment on the job
    assert job.attachments.count() == 1

    # the attachment should be readable as a PDF
    pdf = PdfReader(job.attachments.first().file)
    assert len(pdf.pages) == 1
    page_1 = pdf.pages[0]
    assert 'ASSET INVENTORY' in page_1.extract_text()

    job.refresh_from_db()
    assert job.status == LongRunningJob.Status.DONE
    assert job.progress == 100


@pytest.mark.django_db
def test_export_inventory_by_period(country_fixtures, monkeypatch):
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)

    inventory = create_inventory(
        premises=premises,
        date_start=date(2022, 1, 1),
        date_end=date(2022, 6, 30),
    )

    def mock_inventory_to_pdf(inventory):
        return b'Fake PDF content'

    monkeypatch.setattr('logistics.tasks.assets_inventory_export._inventory_to_pdf', mock_inventory_to_pdf)

    from logistics.tasks.assets_inventory_export import export_inventory

    zip_file_path = export_inventory(
        export_type='period',
        start_date=date(2022, 1, 1),
        end_date=date(2022, 6, 30),
    )

    assert os.path.exists(zip_file_path)
    assert os.path.getsize(zip_file_path) > 0

    with zipfile.ZipFile(zip_file_path, 'r') as zip_file:
        file_list = zip_file.namelist()
        assert len(file_list) >= 1
        assert any(f'inventory_{inventory.code}.pdf' in file for file in file_list)

    os.remove(zip_file_path)


@pytest.mark.django_db
def test_export_inventory_by_project(country_fixtures, monkeypatch):
    premises = create_premises()
    asset = create_asset()
    create_asset_allocation_premises(asset=asset, premises=premises)

    inventory = create_inventory(premises=premises)

    project_contract = create_project_contract()

    asset.current_project_contract = project_contract
    asset.save()

    def mock_inventory_to_pdf(inventory):
        return b'Fake PDF content'

    monkeypatch.setattr('logistics.tasks.assets_inventory_export._inventory_to_pdf', mock_inventory_to_pdf)

    from logistics.tasks.assets_inventory_export import export_inventory

    zip_file_path = export_inventory(
        export_type='project',
        project_contract_id=project_contract.id,
    )

    assert os.path.exists(zip_file_path)
    assert os.path.getsize(zip_file_path) > 0

    with zipfile.ZipFile(zip_file_path, 'r') as zip_file:
        file_list = zip_file.namelist()
        assert len(file_list) >= 1
        assert any(f'inventory_{inventory.code}.pdf' in file for file in file_list)

    os.remove(zip_file_path)
