import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import web

from channel.web import database as db, web_channel
from channel.web.push import repository
from channel.web.push.catalog import content_categories, GREETING_WINDOWS


@pytest.fixture
def admin_app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'soul.db'))
    db.init_db()
    monkeypatch.setattr(web_channel, 'COMPLAINT_ADMIN_PASSCODE', 'admin-ui-test')
    return web.application((
        '/api/admin/push-contents/catalog', 'PushContentCatalogHandler',
        '/api/admin/push-contents', 'PushContentCollectionHandler',
        r'/api/admin/push-contents/(\d+)', 'PushContentItemHandler',
        r'/api/admin/push-contents/(\d+)/images', 'PushContentImageCollectionHandler',
    ), web_channel.__dict__)


def request(app, path, method='GET', data=None):
    return app.request('/api/admin/push-contents' + path, method=method,
                       headers={'X-Admin-Passcode': 'admin-ui-test', 'Content-Type': 'application/json'},
                       data=json.dumps(data) if data is not None else None)


def test_catalog_includes_empty_scenes_and_full_counts_without_oss_credentials(admin_app):
    result = request(admin_app, '/catalog')
    assert result.status == '200 OK'
    categories = json.loads(result.data)['data']['categories']
    greeting = categories[0]
    assert [group['label'] for group in greeting['groups']] == ['早间问候', '午间问候', '晚间问候']
    assert sum(len(group['scenes']) for group in greeting['groups']) == 17
    with db.get_db() as conn:
        conn.execute("DELETE FROM push_content WHERE delivery_scene='GREETING_0730'")
        conn.commit()
    catalog = repository.get_content_catalog()
    breakfast = catalog['categories'][0]['groups'][0]['scenes'][1]
    assert breakfast['label'] == '07:30–07:59'
    assert breakfast['total'] == 0
    assert breakfast['enabledCount'] == 0
    assert admin_app.request('/api/admin/push-contents/catalog').status == '401 Unauthorized'


def test_new_content_gets_scene_specific_number_and_can_be_filtered(admin_app):
    data = dict(pushType='greeting', deliveryScene='GREETING_0730', title='早餐准备好了', body='记得吃早餐', enabled=True)
    result = request(admin_app, '', 'POST', data)
    assert result.status == '200 OK'
    content_id = json.loads(result.data)['data']['id']
    content = repository.get_content(content_id)
    assert content['content_no'] == 'AM-0730-15'
    result = request(admin_app, '?pushType=greeting&deliveryScene=GREETING_0730&keyword=早餐准备好了')
    assert json.loads(result.data)['data']['items'][0]['id'] == content_id
    data.update(body='更新后的正文', enabled=False)
    assert request(admin_app, '/' + str(content_id), 'PUT', data).status == '200 OK'
    assert repository.get_content(content_id)['content_no'] == 'AM-0730-15'
    breakfast = repository.get_content_catalog()['categories'][0]['groups'][0]['scenes'][1]
    assert breakfast['total'] == 15 and breakfast['enabledCount'] == 14


@pytest.mark.parametrize('push_type,scene', [('greeting','GREETING_2359'), ('weather','WEATHER_TYP0'), ('diary','DIARY_OTHER'), ('recall','RECALL_08'), ('weather','GREETING_0730')])
def test_invalid_scene_cannot_be_saved(admin_app, push_type, scene):
    response = request(admin_app, '', 'POST', dict(pushType=push_type, deliveryScene=scene, title='标题', body='正文', enabled=True))
    assert response.status == '400 Bad Request'


def test_unknown_existing_scenes_remain_visible_for_correction(admin_app):
    repository.create_content('OLD-01', 'greeting', 'GREETING_9999', '旧文案', '正文')
    group = repository.get_content_catalog()['categories'][0]['groups'][-1]
    assert group['label'] == '待归类的历史数据'
    assert group['scenes'][0]['legacy'] is True
    assert group['scenes'][0]['total'] == 1


def test_concurrent_creates_do_not_reuse_automatic_numbers(admin_app):
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: repository.create_content('', 'weather', 'WEATHER_GALE', '大风提醒', '正文'), range(4)))
    assert len({repository.get_content(value)['content_no'] for value in ids}) == 4


def test_real_multipart_upload_reaches_image_service(admin_app, monkeypatch):
    seen = []
    def upload(content_id, filename, data):
        seen.append((content_id, filename, data))
        return {'imageId': 42, 'imageUrl': 'https://example.com/test.png'}
    monkeypatch.setattr(web_channel.push_assets, 'upload_image_for_content', upload)
    body = (b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="test.png"\r\n'
            b'Content-Type: image/png\r\n\r\nimage-data\r\n--fixture--\r\n')
    response = admin_app.request('/api/admin/push-contents/1/images', method='POST',
        headers={'X-Admin-Passcode': 'admin-ui-test', 'Content-Type': 'multipart/form-data; boundary=fixture'}, data=body.decode('ascii'))
    assert json.loads(response.data)['success'] is True
    assert seen == [(1, 'test.png', b'image-data')]


def test_editor_greeting_options_match_scheduler_windows():
    categories = content_categories()
    windows = {scene['value'].removeprefix('GREETING_') for group in categories[0]['groups'] for scene in group['scenes']}
    assert windows == {window for period in GREETING_WINDOWS.values() for window in period}
