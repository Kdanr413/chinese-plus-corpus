import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from corpus_delivery.search import create_app
from corpus_delivery.catalog import ReleaseCatalog
from corpus_delivery.export import export_release

class FakeService:
    def list_corpora(self): return []
    def search(self, *args, **kwargs): raise RuntimeError('private server configuration')

class DeliveryTests(unittest.TestCase):
    def test_only_query_routes_are_registered(self):
        client=TestClient(create_app(Path('.'),service=FakeService()),raise_server_exceptions=False)
        self.assertEqual(client.get('/health').status_code,200)
        page=client.get('/search').text
        self.assertNotIn('语料库构建</a>',page)
        self.assertNotIn('导出当前版本结构化语料</a>',page)
        for path in ['/api/upload','/api/run','/api/preview','/api/download','/api/bootstrap','/topic-workflow','/openapi.json','/api/v1/exports/structured']:
            for method in ['get','post']:
                self.assertIn(getattr(client,method)(path).status_code,(404,405))
        error=client.get('/api/v1/search?q=test')
        self.assertEqual(error.status_code,503)
        self.assertNotIn('private server',error.text)
        self.assertEqual(client.get('/api/v1/kwic?q=test&limit=101').status_code,422)
    def test_invalid_export_and_empty_catalog(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.assertEqual(ReleaseCatalog(root).list_corpora(),[])
            with self.assertRaises(ValueError): export_release(root,root/'out')

    def test_export_is_relocatable_and_tampering_is_rejected(self):
        from corpus_releases import create_workspace, create_release
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            workspace=create_workspace(root/'workspaces','demo','run')
            (workspace.path/'corpus'/'text.txt').write_text('Doc 示例\nItem: 电池。\n',encoding='gb18030')
            (workspace.path/'index'/'Org.dat').write_bytes(b'fixture-index')
            release=create_release(workspace.path,root/'source',corpus_id='demo',version='1.0',corpus_format_version='1.0',pipeline_version='1.0',search_engine_version='LangSC.BCC 2.0.7')
            export_release(release.path,root/'moved')
            catalog=ReleaseCatalog(root/'moved')
            corpus=catalog.get_corpus('demo')
            version=catalog.get_version('demo')
            self.assertEqual(version['release_path'],'demo/1.0')
            self.assertEqual(len(catalog.list_versions(str(corpus['id']))),1)
            self.assertTrue(catalog.verify_registered_files('demo','1.0')['valid'])
            with self.assertRaises(FileExistsError): export_release(release.path,root/'moved')
            (root/'moved/demo/1.0/corpus/text.txt').write_bytes(b'changed')
            with self.assertRaises(ValueError): ReleaseCatalog(root/'moved')

if __name__=='__main__': unittest.main()
