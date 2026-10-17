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

 def test_top_ten_and_full_export_with_filters(self):
  self.assets.annotations.write_text(json.dumps({'document_id':'u','source_document_id':'d1','clean_text':' '.join('术语'+str(i) for i in range(15)),'terms':['术语'+str(i) for i in range(15)],'topics':['技术 > 储能 > 电池']}),encoding='utf-8')
  with patch('corpus_api.knowledge.PROJECT_ROOT',self.root),patch.dict('os.environ',{'ADMIN_API_TOKEN':'test-only'}):
   client=TestClient(create_app(lambda:object()));h={'x-admin-token':'test-only'}
   result=client.get('/api/v1/terms?dataset=workspace',headers=h).json();self.assertEqual(result['page_size'],10);self.assertEqual(len(result['items']),10);self.assertEqual(result['total'],15)
   response=client.get('/api/v1/terms/export?dataset=workspace&source_category=教材',headers=h)
   rows=list(csv.DictReader(__import__('io').StringIO(response.content.decode('utf-8-sig'))));self.assertEqual(len(rows),15)
   self.assertEqual(client.get('/api/v1/terms/export?dataset=workspace').status_code,403)

 def test_topic_names_do_not_use_keyword_lists(self):
  from corpus_api.topic_display import TopicDisplay
  self.assets.taxonomy.write_text(json.dumps({'nodes':[{'id':'test','name':'BT_TOPIC_021｜电池、储能','path':'BERTopic测试体系 > 聚类结果 > BT_TOPIC_021｜电池、储能','keywords':['电池','储能']},{'id':'formal','name':'动力电池','path':'技术 > 储能 > 动力电池','keywords':['锂离子电池']}]}),encoding='utf-8')
  display=TopicDisplay(self.root,True)
  self.assertEqual(display.resolve('电池、储能')['name'],'待命名聚类 BT_TOPIC_021')
  self.assertEqual(display.resolve('技术 > 储能 > 动力电池')['name'],'动力电池')
  self.assertEqual(display.nodes[0]['keywords'],['电池','储能'])

 def test_upload_sources_inherit_and_snapshot_without_mutating_old_release(self):
  from corpus_api.source_metadata import save_upload_sources,validate_source
  docs=[{'doc_id':'d1','title':'课本','original_path':str(self.root/'data/raw/manual/a.txt')}]
  self.assets.doc_path.write_text(json.dumps(docs[0]),encoding='utf-8')
  with self.assertRaises(ValueError):validate_source('教材','')
  with self.assertRaises(ValueError):save_upload_sources(self.root,['../other.txt'],'论文','')
  save_upload_sources(self.root,['data/raw/manual/a.txt'],'教材','课本第一册')
  self.assertEqual(self.assets.documents()['d1']['book_title'],'课本第一册')
  snapshot_knowledge(self.root,self.release/'metadata/knowledge',['d1'])
  save_upload_sources(self.root,['data/raw/manual/a.txt'],'论文','')
  self.assertEqual(KnowledgeAssets(self.release).documents()['d1']['source_category'],'教材')
  self.assertEqual(self.assets.documents()['d1']['source_category'],'论文')


class UploadBatchSourceTests(unittest.TestCase):
 def test_textbook_txt_and_zip_derive_each_book_name_and_keep_batches_separate(self):
  import io,zipfile
  from corpus_api.source_metadata import upload_source_map
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder)
   (root/'configs').mkdir()
   (root/'configs/pipeline.json').write_text('{}',encoding='utf-8')
   with patch('corpus_api.workbench.PROJECT_ROOT',root),patch('corpus_api.workbench.legacy.PROJECT_ROOT',root),patch.dict('os.environ',{'ADMIN_API_TOKEN':'test-only'}):
    client=TestClient(create_app(lambda:object()));headers={'x-admin-token':'test-only'}
    def upload(filename,data,kind):
     response=client.post('/api/upload',params={'category':'corpus','filename':filename,'source_category':kind},content=data,headers=headers)
     self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['ok'],response.text)
    upload('教材甲.txt','第一本教材正文。'.encode(),'教材')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:
     z.writestr('课程/教材乙.txt','第二本教材正文。'.encode());z.writestr('教材丙.txt','第三本教材正文。'.encode())
    upload('多本教材.zip',archive.getvalue(),'教材')
    upload('报道.txt','新闻正文。'.encode(),'新闻')
    values=list(upload_source_map(root).values())
    self.assertEqual({row['book_title'] for row in values if row['source_category']=='教材'},{'教材甲','教材乙','教材丙'})
    self.assertEqual(len(values),4)
    news=[row for row in values if row['source_category']=='新闻'];self.assertEqual(len(news),1);self.assertEqual(news[0]['book_title'],'')
    for row in values:self.assertTrue((root/row['path']).is_file())
    invalid=client.post('/api/upload',params={'category':'corpus','filename':'不应写入.txt','source_category':'错误类别'},content=b'test',headers=headers)
    self.assertEqual(invalid.status_code,400)
    self.assertFalse(list((root/'data/raw').rglob('不应写入.txt')))
