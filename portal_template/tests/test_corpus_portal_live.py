import sys,uuid,tempfile,secrets,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from corpus_portal.app import create_app
from corpus_portal.store import PortalStore
from corpus_portal.workspaces import PortalSettings,ROOT
from corpus_portal.supervisor import WorkerSupervisor
from corpus_registry.db import connect
import unittest

def run_check():
    load_dotenv(ROOT/'.env',override=False)
    store=PortalStore();store.initialize()
    users=[];projects=[];schemas=[]
    suffix=uuid.uuid4().hex[:12]
    password=secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix='corpus-portal-check-') as tmp:
        settings=PortalSettings(Path(tmp)/'storage',ROOT/'portal_template')
        supervisor=WorkerSupervisor(settings)
        try:
            for label in ['a','b']: users.append(store.add_user('check_'+suffix+'_'+label,password))
            app=create_app(settings,store,supervisor)
            a=TestClient(app,raise_server_exceptions=False);b=TestClient(app,raise_server_exceptions=False)
            clients=[a,b];headers=[]
            for index,client in enumerate(clients):
                result=client.post('/_portal/api/login',json={'username':'check_'+suffix+'_'+['a','b'][index],'password':password})
                assert result.status_code==200,result.text
                headers.append({'x-csrf-token':result.json()['csrf_token']})
                result=client.post('/_portal/api/projects',json={'name':'integration '+str(index)},headers=headers[index])
                assert result.status_code==201,result.text
                projects.append(uuid.UUID(result.json()['id']));schemas.append('p_'+projects[-1].hex)
            for index,client in enumerate(clients):
                pid=projects[index];prefix=f'/p/{pid}'
                result=client.get(prefix+'/api/bootstrap')
                assert result.status_code==200,result.text
                assert result.json()['project']['name']=='integration '+str(index),result.text
                page=client.get(prefix+'/')
                assert page.status_code==200 and 'portalShareButton' in page.text and 'portalShareFrame' in page.text,page.text[:200]
                assert client.get(f'/p/{projects[1-index]}/api/bootstrap').status_code==404
                result=client.post(prefix+'/api/upload?category=corpus&filename=private-'+str(index)+'.txt',content=('only-user-'+str(index)).encode(),headers={**headers[index],'content-type':'application/octet-stream'})
                assert result.status_code==200 and result.json().get('ok'),result.text
                result=client.get(prefix+'/api/raw-corpus')
                assert result.status_code==200,result.text
                text=result.text
                assert 'private-'+str(index)+'.txt' in text,text
                assert 'private-'+str(1-index)+'.txt' not in text,text
                result=client.post(prefix+'/api/workflow/rounds',json={'source_path':str(settings.project_root(projects[1-index])/('data/raw/manual/private-'+str(1-index)+'.txt'))},headers=headers[index])
                assert result.status_code==400,result.text
                topic=client.get(prefix+'/api/workflow/bootstrap')
                assert topic.status_code==200,topic.text
                recommended=topic.json()['execution']['python_executable']
                probe=client.post(prefix+'/api/workflow/rounds/R9999/cluster',json={'python_executable':recommended},headers=headers[index])
                assert probe.status_code==400 and 'detail' in probe.json() and '不存在' in probe.json()['detail'],probe.text
                import httpx
                worker=supervisor.workers[pid]
                assert httpx.get(f'http://127.0.0.1:{worker.port}/health',trust_env=False).status_code==403
            with connect() as c:
                for schema in schemas:
                    n=c.execute('SELECT count(*) AS n FROM '+schema+'.corpora').fetchone()['n']
                    assert n==1
            print('PASS: PostgreSQL authentication, two real workers, isolated uploads/project metadata, cross-owner denial, direct-worker denial')
        finally:
            supervisor.close()
            with connect() as c:
                if projects: c.execute('DELETE FROM corpus_portal.projects WHERE id=ANY(%s)',(projects,))
                if users:
                    c.execute('DELETE FROM corpus_portal.sessions WHERE user_id=ANY(%s)',(users,))
                    c.execute('DELETE FROM corpus_portal.users WHERE id=ANY(%s)',(users,))
                from corpus_portal.security import digest
                keys=[digest('name:check_'+suffix+'_'+x) for x in ['a','b']]
                c.execute('DELETE FROM corpus_portal.login_attempts WHERE key_hash=ANY(%s)',(keys,))
                for schema in schemas:
                    assert schema in ['p_'+p.hex for p in projects]
                    c.execute('DROP SCHEMA IF EXISTS '+schema+' CASCADE')

@unittest.skipUnless(os.getenv("CORPUS_PORTAL_LIVE_TEST")=="1","requires PostgreSQL and explicit live-test flag")
class PortalLiveTests(unittest.TestCase):
    def test_two_real_workers(self): run_check()

if __name__=="__main__": unittest.main()
