"""Explicit, offline two-user acceptance; leaves generated assets and a JSON report.
Creates temporary accounts/schemas, removes only those records in finally.
Requires PostgreSQL, native BCC, BERTopic interpreter and cached embedding weights.
Uses TEST taxonomy publication and a local synthetic syntax fixture; neither
formal model review nor real syntax quality is verified.
"""
from pathlib import Path
import sys,os,json,uuid,time,secrets,traceback
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from corpus_portal.app import create_app
from corpus_portal.store import PortalStore
from corpus_portal.workspaces import ROOT,PortalSettings
from corpus_portal.supervisor import WorkerSupervisor
from corpus_registry.db import connect
if os.environ.get('CORPUS_PORTAL_ACCEPTANCE') != '1':
 raise SystemExit('Set CORPUS_PORTAL_ACCEPTANCE=1 to run the real offline database/model acceptance.')
load_dotenv(ROOT/'.env',override=False)
os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TERM_RECOGNITION_BCC_OFFLINE='1')
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
class SyntaxFixture(BaseHTTPRequestHandler):
 def do_POST(self):
  size=int(self.headers.get('Content-Length','0'))
  if self.path!='/struct_by_tokens' or size>1024*1024:
   self.send_error(400);return
  data=json.loads(self.rfile.read(size))
  assert len(data['words'])==len(data['pos'])
  def safe(value):return str(value).replace('(', '（').replace(')', '）').replace(' ', '')
  tree='(S '+' '.join('('+safe(pos)+' '+safe(word)+')' for word,pos in zip(data['words'],data['pos']))+')'
  body=json.dumps({'tree':tree},ensure_ascii=False).encode('utf-8')
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
syntax_fixture=ThreadingHTTPServer(('127.0.0.1',0),SyntaxFixture)
threading.Thread(target=syntax_fixture.serve_forever,daemon=True).start()
os.environ['CORPUS_SYNTAX_TREE_BASE_URL']='http://127.0.0.1:'+str(syntax_fixture.server_port)
run_id=uuid.uuid4().hex
storage=ROOT/'data/pa'
settings=PortalSettings(storage,ROOT/'portal_template',max_workers=2)
store=PortalStore();store.initialize();supervisor=WorkerSupervisor(settings)
users=[];pids=[];report={'run_id':run_id,'mode':'offline_test_taxonomy_and_synthetic_syntax','projects':[],'status':'running'}
password=secrets.token_urlsafe(20)
clients=[];auth=[]
def save():
 storage.mkdir(parents=True,exist_ok=True)
 (storage/('acceptance-'+run_id+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
def call(client,path,method='GET',body=None,headers=None):
 r=client.request(method,path,json=body,headers=headers)
 if r.status_code>=400: raise RuntimeError(f'{method} {path}: {r.status_code} {r.text[:1000]}')
 return r.json()
def poll(client,path,done,timeout=600):
 end=time.monotonic()+timeout;last=None
 while time.monotonic()<end:
  data=call(client,path)
  label=data.get('detail') or data.get('job',{}).get('current_step') or data.get('current_step') or data.get('status')
  if label!=last:
   print('PROGRESS',path,label,flush=True);last=label
  if done(data):return data
  time.sleep(1)
 raise TimeoutError(path)
try:
 app=create_app(settings,store,supervisor)
 for i in range(2):
  name='accept_'+run_id[:12]+'_'+str(i);users.append(store.add_user(name,password))
  c=TestClient(app,raise_server_exceptions=False);clients.append(c)
  token=call(c,'/_portal/api/login','POST',{'username':name,'password':password})['csrf_token'];h={'x-csrf-token':token};auth.append(h)
  project=call(c,'/_portal/api/projects','POST',{'name':'离线验收 '+str(i)},h)
  pid=uuid.UUID(project['id']);pids.append(pid)
  prefix='/p/'+str(pid)
  words=['甲组专用电芯','乙组专用光伏'][i]
  bases=[f'{words}用于动力电池储能系统，技术人员测量电芯电压并检查电池管理系统',f'{words}用于太阳能发电系统，工程人员检查光伏组件和逆变器输出功率',f'{words}用于新能源汽车，研发人员测试电机控制器与充电系统的运行性能']
  corpus='\n'.join(base+f'，工程人员检查系统运行性能，这是第{n+1}次工程试验。' for base in bases for n in range(90))
  response=c.post(prefix+'/api/upload?category=corpus&filename=acceptance.txt',content=corpus.encode('utf-8'),headers={**h,'content-type':'application/octet-stream'})
  assert response.status_code==200 and response.json().get('ok'),response.text
  row={'project_id':str(pid),'keyword':words,'steps':[]};report['projects'].append(row);save()
  print('PROJECT',i,'uploaded',flush=True)
  call(c,prefix+'/api/run','POST',{'steps':['cleaning'],'resources':{},'model':{'enabled':False}},h)
  cleaned=poll(c,prefix+'/api/status',lambda d:not d['job']['running'])
  assert not cleaned['job']['error'],cleaned['job']['error'];row['steps'].append('clean');save()
  prepared=call(c,prefix+'/api/workflow/prepare-current','POST',{'force_new':True},h)
  rid=prepared['round']['round_id'];row['round_id']=rid;row['source_units']=prepared['profile']['source_unit_count'];row['sample_size']=prepared['profile']['sample_size'];save()
  topic=call(c,prefix+'/api/workflow/bootstrap');python=topic['execution']['python_executable']
  job=call(c,prefix+f'/api/workflow/rounds/{rid}/cluster','POST',{'python_executable':python},h);jid=job['job_id']
  def cluster_done(d):
   item=next(x for x in d['jobs'] if x['job_id']==jid)
   if item['status']=='failed':raise RuntimeError('clustering: '+str(item.get('message')))
   return item['status']=='completed'
  poll(c,prefix+'/api/workflow/bootstrap',cluster_done,timeout=1200)
  row['steps'].append('real_bertopic');save();print('PROJECT',i,'BERTopic complete',flush=True)
  pub=call(c,prefix+f'/api/workflow/rounds/{rid}/publish-bertopic-test','POST',{'confirm_test_mode':True},h)
  row['steps'].append('test_taxonomy_publish');save()
  call(c,prefix+'/api/v1/recognition/runs','POST',{'topic_table_path':'data/curated/topics/taxonomy.json','topic_mode':'classification','machine_validation':True,'human_validation':False,'use_external_model':False,'include_builtin_rules':False,'cross_validation_enabled':False},h)
  recognized=poll(c,prefix+'/api/v1/recognition/status',lambda d:not d['running'],timeout=900)
  assert recognized['status']=='succeeded',recognized.get('error') or recognized.get('detail')
  call(c,prefix+f"/api/v1/recognition/runs/{recognized['run_id']}/activate",'POST',{},h)
  row['steps'].append('recognition_activate');save()
  call(c,prefix+'/api/run','POST',{'steps':['segmentation','structure','syntax'],'term_score_threshold':0,'resources':{},'topic_review_required':False,'model':{'enabled':False}},h)
  structured=poll(c,prefix+'/api/status',lambda d:not d['job']['running'],timeout=600)
  assert not structured['job']['error'],structured['job']['error'];row['steps'].append('structure_syntax');save()
  call(c,prefix+'/api/v1/platform/publish','POST',{'version':'acceptance-1','make_default':True},h)
  released=poll(c,prefix+'/api/v1/platform/publish-status',lambda d:not d['running'],timeout=600)
  assert not released['error'],released['error'];row['steps'].append('native_index_release');save()
  shared=call(c,f'/_portal/api/projects/{pid}/shares','POST',{'version':'acceptance-1'},h)
  base=shared['url'].split(settings.public_url,1)[1].removesuffix('/search')
  guest=TestClient(app,raise_server_exceptions=False)
  query=call(guest,base+'/api/v1/search?q='+words)
  assert query['result']['Count']>0 and sum(query['result']['Freq'].values())==270,query
  other=call(guest,base+'/api/v1/search?q='+['乙组专用光伏','甲组专用电芯'][i])
  assert other['result']['Count']==0,other
  row['other_user_keyword_count']=other['result']['Count']
  row['query_result']=query;row['steps'].append('anonymous_native_search');save()
  assert guest.post(base+'/api/upload',json={}).status_code in [404,405]
  call(c,'/_portal/api/shares/'+shared['id']+'/revoke','POST',{},h)
  assert guest.get(base+'/search').status_code==404
  row['steps'].append('share_revoke');save();print('PROJECT',i,'FULL OFFLINE CHAIN PASSED',flush=True)
 for i,c in enumerate(clients):
  assert c.get('/p/'+str(pids[1-i])+'/api/bootstrap').status_code==404
 report['status']='passed';save()
except Exception as e:
 report['status']='failed';report['error']=str(e);save();traceback.print_exc();raise
finally:
 supervisor.close()
 syntax_fixture.shutdown();syntax_fixture.server_close()
 with connect() as c:
  if pids:
   c.execute('DELETE FROM corpus_portal.shares WHERE project_id=ANY(%s)',(pids,))
   c.execute('DELETE FROM corpus_portal.projects WHERE id=ANY(%s)',(pids,))
  if users:
   c.execute('DELETE FROM corpus_portal.sessions WHERE user_id=ANY(%s)',(users,))
   c.execute('DELETE FROM corpus_portal.users WHERE id=ANY(%s)',(users,))
  for pid in pids: c.execute('DROP SCHEMA IF EXISTS p_'+pid.hex+' CASCADE')
 print('REPORT',storage/('acceptance-'+run_id+'.json'),flush=True)
