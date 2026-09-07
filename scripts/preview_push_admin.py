"""Local UI integration fixture. Disposable DB and simulated OSS; never loads production config."""
import sys
import os
from pathlib import Path
repository_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository_root))
os.chdir(repository_root)
import tempfile
import web
from wsgiref.simple_server import make_server
from channel.web import database as db, web_channel as handlers
from channel.web.push import assets

preview = tempfile.TemporaryDirectory(prefix='push-admin-preview-')
db.DB_PATH = os.path.join(preview.name, 'soul.db')
db.init_db()
handlers.COMPLAINT_ADMIN_PASSCODE = 'preview-only'
uploads = {}
assets._put_private_oss_object = lambda config, key, data, content_type, **kwargs: uploads.update({key:(data, content_type)})
assets._PushAssetOssConfig.from_runtime = classmethod(lambda cls: cls('preview','preview','preview','https://example.invalid',3600))
assets.create_image_read_url = lambda key, **kwargs: '/preview-image?key=' + key

class PreviewImage:
    def GET(self):
        key = web.input(key='').key
        if key in uploads:
            data, content_type = uploads[key]
        else:
            import zipfile, csv
            with open('docs/push-assets-manifest.csv',encoding='utf-8-sig') as f:
                entry = next((row for row in csv.DictReader(f) if row['object_key'] == key), None)
            if not entry: raise web.notfound()
            with zipfile.ZipFile('push文案+插画.zip') as archive: data=archive.read(entry['source_image'])
            content_type='image/png'
        web.header('Content-Type',content_type)
        return data

scope = dict(handlers.__dict__, PreviewImage=PreviewImage)
app = web.application((
    '/push-contents','PushContentsPageHandler', '/complaints','ComplaintsPageHandler',
    '/api/admin/complaints/auth','ComplaintAdminAuthHandler', '/api/admin/complaints','ComplaintAdminListHandler',
    '/api/admin/push-contents/catalog','PushContentCatalogHandler',
    '/api/admin/push-contents','PushContentCollectionHandler',
    r'/api/admin/push-contents/(\d+)','PushContentItemHandler',
    r'/api/admin/push-contents/(\d+)/images','PushContentImageCollectionHandler',
    r'/api/admin/push-contents/(\d+)/images/(\d+)','PushContentImageItemHandler',
    '/assets/(.*)','AssetsHandler', '/preview-image','PreviewImage',
), scope)
print('Preview http://127.0.0.1:9876/push-contents ; isolated database ; password preview-only',flush=True)
try:
    make_server('127.0.0.1',9876,app.wsgifunc()).serve_forever()
finally:
    preview.cleanup()
