import hashlib,json,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from corpus_releases import create_workspace,create_release
from corpus_delivery.public_site import create_app,verify_source_bundle


class Engine:
 def frequency(self,q,**options):return {"Freq":{q:2}}
 def kwic(self,q,**options):return {"Total":1,"Context":[{"Source":"example","Context":q}]}


class PublicSiteTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  workspace=create_workspace(self.root/'workspaces','demo','one')
  (workspace.path/'corpus/text.txt').write_text('Doc 示例\n#Global 领域=测试\n#Group\nItem: 动力电池。\n',encoding='gb18030')
  (workspace.path/'index/Org.dat').write_bytes(b'fixture-index')
  knowledge=workspace.path/'metadata/knowledge';knowledge.mkdir(parents=True)
  (knowledge/'taxonomy.json').write_text(json.dumps({'taxonomy_version':'3','nodes':[{'id':'a','name':'动力电池','path':'技术 > 储能 > 动力电池','keywords':['锂离子电池']}]}),encoding='utf-8')
  (knowledge/'documents.jsonl').write_text(json.dumps({'doc_id':'d1','title':'测试教材','source_category':'教材','book_title':'测试教材'})+'\n',encoding='utf-8')
  self.release=create_release(workspace.path,self.root/'releases',corpus_id='demo',version='1.0',corpus_format_version='1',pipeline_version='1',search_engine_version='LangSC.BCC 2.0.7')
  create_release(workspace.path,self.root/'releases',corpus_id='demo',version='private-old',corpus_format_version='1',pipeline_version='1',search_engine_version='LangSC.BCC 2.0.7')
  self.bundle=self.root/'bundle/corpus-source.zip';self.bundle.parent.mkdir()
  self.make_bundle({'README.md':'example source','corpus_api/example.py':'# example'})
  templates=self.root/'参考文件中心/模板';templates.mkdir(parents=True)
  for name in ['参考话题体系_填写示例.json','规范术语表.csv','语料来源登记.csv']:(templates/name).write_text('{}' if name.endswith('.json') else 'name\n',encoding='utf-8')
  self.config={'site_title':'公开语料库','release_root':'releases','allowed_releases':[{'corpus_key':'demo','version':'1.0'}],'source_archive':'bundle/corpus-source.zip'}
  self.app=create_app(self.config,project_root=self.root,engine_loader=lambda index:Engine());self.client=TestClient(self.app,raise_server_exceptions=False)
 def make_bundle(self,files):
  with zipfile.ZipFile(self.bundle,'w') as z:
   for name,content in files.items():z.writestr(name,content)
  digest=hashlib.sha256(self.bundle.read_bytes()).hexdigest()
  self.bundle.with_name('release-manifest.json').write_text(json.dumps({'mode':'unified-source','sha256':digest,'bytes':self.bundle.stat().st_size,'private_findings':0,'raw_data_included':False,'user_accounts_included':False,'runtime_included':False}),encoding='utf-8')
 def test_home_workflow_download_and_api_are_distinct(self):
  self.assertIn('选择你的使用方式',self.client.get('/').text)
  self.assertIn('建立或复用话题体系',self.client.get('/workflow').text)
  self.assertIn('下载完整构建代码',self.client.get('/download').text)
  self.assertIn('/api/v1/kwic',self.client.get('/api-guide').text)
  search=self.client.get('/search');self.assertIn('href="/">首页',search.text);self.assertNotIn('语料库构建</a>',search.text)
  self.assertEqual(self.client.get('/assets/public-site.css').status_code,200)
  self.assertEqual(self.client.get('/api/v1/search?q=电池').status_code,200)
  self.assertEqual(self.client.get('/api/v1/kwic?q=电池').status_code,200)
  info=self.client.get('/api/site').json();self.assertEqual(info['versions'][0]['topic_count'],1);self.assertEqual(info['versions'][0]['document_count'],1)
 def test_only_selected_release_is_exposed_and_no_database_is_used(self):
  result=self.client.get('/api/v1/corpora').json();cid=result['items'][0]['id']
  versions=self.client.get('/api/v1/corpora/'+cid+'/versions').json()['items']
  self.assertEqual([v['version'] for v in versions],['1.0'])
  self.assertEqual(self.client.get('/api/v1/terms?version=private-old').status_code,404)
  with patch('corpus_registry.db.connect',side_effect=AssertionError('must not use DB')):
   self.assertEqual(self.client.get('/api/v1/topics?level=3').status_code,200)
 def test_download_is_exact_source_bundle_and_no_private_routes_exist(self):
  response=self.client.get('/downloads/builder-source.zip');self.assertEqual(response.content,self.bundle.read_bytes())
  self.assertEqual(response.headers['x-checksum-sha256'],hashlib.sha256(response.content).hexdigest())
  self.assertIn(response.headers['x-checksum-sha256'],self.client.get('/downloads/SHA256SUMS.txt').text)
  self.assertEqual(self.client.get('/resources/topic-taxonomy.json').status_code,200)
  self.assertEqual(self.client.get('/resources/sample-corpus.txt').status_code,200)
  for path in ['/_portal','/_portal/api/projects','/api/upload','/api/run','/api/preview','/api/download','/api/workflow/bootstrap','/api/v1/exports/structured','/.env','/resources/.env','/resources/..%2F.env']:
   for method in ['get','post']:self.assertIn(getattr(self.client,method)(path).status_code,[404,405])
  self.assertEqual(self.client.get('/api/v1/annotations?dataset=workspace').status_code,403)
  spec=self.client.get('/openapi.json').json();self.assertTrue(all(set(v)<={'get','parameters'} for v in spec['paths'].values()))
 def test_modified_bundle_is_not_served_and_private_archives_are_rejected(self):
  self.bundle.write_bytes(b'changed-private-file')
  response=self.client.get('/downloads/builder-source.zip');self.assertEqual(response.status_code,503);self.assertNotIn('changed-private-file',response.text)
  for name in ['.env','data/raw/example.txt','../escape.py','portal_template/.env','runtime/python.exe']:
   self.make_bundle({name:'private'})
   with self.subTest(name=name),self.assertRaises(ValueError):verify_source_bundle(self.bundle)
 def test_empty_allowlist_does_not_publish_other_releases(self):
  client=TestClient(create_app({**self.config,'allowed_releases':[],'source_archive':''},project_root=self.root))
  self.assertEqual(client.get('/api/v1/corpora').json()['count'],0)
  self.assertEqual(client.get('/downloads/builder-source.zip').status_code,404)
  self.assertIn('等待配置公开版本',client.get('/').text)
 def test_missing_selected_version_and_unsafe_repository_are_rejected(self):
  with self.assertRaises(ValueError):create_app({**self.config,'allowed_releases':[{'corpus_key':'demo','version':'missing'}]},project_root=self.root)
  with self.assertRaises(ValueError):create_app({**self.config,'repository_url':'javascript:alert(1)'},project_root=self.root)
  client=TestClient(create_app({**self.config,'site_title':'<script>bad</script>'},project_root=self.root))
  self.assertNotIn('<script>bad</script>',client.get('/').text)

 def test_unified_entry_dispatches_without_starting_builder_or_registry(self):
  from corpus_platform.cli import main
  with patch('corpus_delivery.public_site.main',return_value=0) as run,patch('corpus_platform.cli.PlatformCoordinator',side_effect=AssertionError('must not start builder')):
   self.assertEqual(main(['public-site','--config','example.json','--host','127.0.0.1','--port','8012']),0)
   self.assertEqual(run.call_args.args[0],['--config','example.json','--host','127.0.0.1','--port','8012'])
