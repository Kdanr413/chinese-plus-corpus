from pathlib import Path
import csv,json,tempfile,unittest,uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from corpus_api.app import create_app
from corpus_api.knowledge_assets import KnowledgeAssets,snapshot_knowledge
from corpus_api.source_search import selected_ranges,scoped_query
from corpus_api.service import SearchRequestError

class KnowledgeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.release=self.root/'release';self.release.mkdir()
  (self.root/'configs').mkdir();(self.root/'configs/pipeline.json').write_text(json.dumps({'project':{'corpus_id':'demo'}}))
  a=KnowledgeAssets(self.root,True)
  for f in (a.annotations,a.taxonomy,a.terminology,a.doc_path,a.mapping):f.parent.mkdir(parents=True,exist_ok=True)
  docs=[{'doc_id':'d1','title':'教材甲','source_category':'教材','book_title':'教材甲'},{'doc_id':'d2','title':'报道乙','source_category':'新闻'},{'doc_id':'d3','title':'报告','source':'book_other'}]
  a.doc_path.write_text('\n'.join(json.dumps(x) for x in docs),encoding='utf-8')
  annotations=[{'document_id':'u1','source_document_id':'d1','source_sentence_id':'s1','clean_text':'电池电池，能源','terms':['电池','能源'],'topics':['技术 > 储能 > 电池'],'provenance':{'recognition_run_id':'r1'}},{'document_id':'u2','source_document_id':'d2','source_sentence_id':'s2','clean_text':'电池新闻','terms':['电池'],'topics':['技术 > 储能 > 电池']}]
  a.annotations.write_text('\n'.join(json.dumps(x) for x in annotations),encoding='utf-8');a.taxonomy.write_text(json.dumps({'taxonomy_version':'3','source_round_id':'R4','nodes':[{'id':'T1','path':'技术 > 储能 > 电池','name':'电池'}]}),encoding='utf-8');a.terminology.write_text('term_id,preferred_term\nTERM1,电池\nTERM2,能源\n',encoding='utf-8');self.assets=a
 def tearDown(self):self.temp.cleanup()
 def test_annotations_and_frequency_filters(self):
  r=list(self.assets.iter_annotations('教材','教材甲'));self.assertEqual(len(r),1);self.assertEqual(r[0]['terms'][0]['term_id'],'TERM1');self.assertEqual(r[0]['topics'][0]['topic_id'],'T1')
  r=self.assets.summary();self.assertEqual(r['terms'][0]['frequency'],3);self.assertEqual(r['terms'][0]['sentence_frequency'],2);self.assertEqual(self.assets.summary('新闻')['terms'][0]['frequency'],1)
  self.assertEqual(self.assets.documents()['d3']['source_category'],'未分类')
 def test_version_snapshot_isolated_and_selected(self):
  snapshot_knowledge(self.root,self.release/'metadata/knowledge',['d1']);a=KnowledgeAssets(self.release);self.assertEqual(a.summary()['sentence_count'],1)
  self.assets.annotations.write_text('',encoding='utf-8');self.assertEqual(a.summary()['sentence_count'],1)
 def test_public_cannot_read_workspace_or_write_sources(self):
  from corpus_delivery.search import create_app as shared
  app=shared(self.release,service=object())
  with TestClient(app) as client:
   self.assertEqual(client.get('/api/v1/annotations?dataset=workspace').status_code,403)
   self.assertEqual(client.post('/api/v1/source-metadata',json={'items':[]}).status_code,404)
 def test_api_auth_pagination_and_book_validation(self):
  with patch('corpus_api.knowledge.PROJECT_ROOT',self.root),patch.dict('os.environ',{'ADMIN_API_TOKEN':'test-only'}):
   client=TestClient(create_app(lambda:object()))
   self.assertEqual(client.get('/api/v1/annotations?dataset=workspace').status_code,403)
   headers={'x-admin-token':'test-only'}
   r=client.get('/api/v1/annotations?dataset=workspace&page_size=1',headers=headers).json();self.assertEqual(r['total'],2);self.assertEqual(len(r['items']),1)
   r=client.get('/api/v1/topics?dataset=workspace',headers=headers).json();self.assertEqual(r['taxonomy_version'],'3');self.assertEqual(r['items'][0]['sentence_frequency'],2)
   bad={'items':[{'document_id':'d2','source_category':'教材','book_title':''}]};self.assertEqual(client.post('/api/v1/source-metadata',json=bad,headers=headers).status_code,400)
   bad['items'][0]['book_title']='教材乙';self.assertEqual(client.post('/api/v1/source-metadata',json=bad,headers=headers).status_code,200);self.assertEqual(self.assets.documents()['d2']['book_title'],'教材乙')
   self.assertEqual(client.get('/api/v1/terms?dataset=workspace&source_category=other',headers=headers).status_code,400)
 def test_scope_mapping_fail_closed_and_book_selection(self):
  snapshot_knowledge(self.root,self.release/'metadata/knowledge');selection={'corpus_files':[{'document_id':'d1','bcc_document_id':'b1','file':'a.txt'},{'document_id':'d2','bcc_document_id':'b2','file':'b.txt'}]};(self.release/'metadata/selection.json').write_text(json.dumps(selection))
  with self.assertRaises(SearchRequestError):selected_ranges(self.release,'教材','教材甲')
  (self.release/'index').mkdir();(self.release/'index/scope_indexes.json').write_text(json.dumps({'scopes':[{'start':0,'end':10,'source_name':'a.txt'},{'start':10,'end':20,'source_name':'b.txt'}]}))
  self.assertEqual(selected_ranges(self.release,'教材','教材甲'),[(0,10)]);self.assertEqual(selected_ranges(self.release,'论文',''),[])
 def test_openapi_project_prefix(self):
  from corpus_portal.rewrite import rewrite_response
  result=json.loads(rewrite_response(json.dumps({'openapi':'3.1.0','paths':{'/api/v1/terms':{}}}).encode(),'application/json','/p/example'))
  self.assertEqual(result['servers'],[{'url':'/p/example'}]);self.assertIn('/api/v1/terms',result['paths'])
 def test_multi_scope_frequency_and_global_kwic_pagination(self):
  class Engine:
   def frequency(self,q,**opts):return {'Freq':{'词':2 if opts['scope_ranges'][0]==(0,10) else 3}}
   def kwic(self,q,**opts):
    base='A' if opts['scope_ranges'][0]==(0,10) else 'B';rows=[{'Context':base+str(i)} for i in range(3)];n=opts['number'];start=n*opts['page_no'];return {'Total':3,'Context':rows[start:start+n]}
  engine=Engine();ranges=[(0,10),(10,20)];self.assertEqual(scoped_query(engine,'q','Freq',{'number':1},ranges)['Freq']['词'],5)
  r=scoped_query(engine,'q','Context',{'number':2,'page_no':1},ranges);self.assertEqual(r['Total'],6);self.assertEqual([x['Context'] for x in r['Context']],['A2','B0'])
