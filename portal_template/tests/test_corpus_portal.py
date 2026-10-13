import base64
import json
import secrets
import tempfile
import unittest
import uuid
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import MagicMock,patch,AsyncMock
from fastapi.testclient import TestClient
from corpus_portal.app import create_app,COOKIE
from corpus_portal.workspaces import PortalSettings,verify_payload,initialize_workspace
from corpus_portal.security import password_hash,password_matches,digest
from corpus_portal.rewrite import rewrite_response
from corpus_delivery.catalog import ReleaseCatalog
from corpus_delivery.search import create_app as search_app
from corpus_api.service import CorpusService,SearchEngineCache
from corpus_releases import create_workspace,create_release

class PayloadErrorsTests(unittest.TestCase):
    def test_model_host_error_is_specific_without_credentials(self):
        from corpus_portal.workspaces import PayloadValidationError,validate_json_payload
        body=json.dumps({'models':[{'model':'DeepSeek-V4.1-Flash','base_url':'https://model.example/v1','api_key':'PRIVATE-TEST-KEY'}]})
        with patch.dict('os.environ',{'PORTAL_LLM_ALLOWED_HOSTS':'api.openai.com'}):
            with self.assertRaises(PayloadValidationError) as failure:
                validate_json_payload(body,Path.cwd(),set())
        message=str(failure.exception)
        self.assertIn('model.example',message)
        self.assertIn('PORTAL_LLM_ALLOWED_HOSTS',message)
        self.assertNotIn('PRIVATE-TEST-KEY',message)

    def test_permitted_provider_model_name_and_malformed_address(self):
        from corpus_portal.workspaces import PayloadValidationError,validate_json_payload
        with patch.dict('os.environ',{'PORTAL_LLM_ALLOWED_HOSTS':'model.example'}):
            validate_json_payload(json.dumps({'models':[{'model':'DeepSeek-V4.1-Flash','base_url':'https://model.example/v1'}]}),Path.cwd(),set())
            for address in ['https://model.example:bad/v1','https://user:SECRET@model.example/v1','http://model.example/v1']:
                with self.assertRaises(PayloadValidationError) as failure:
                    validate_json_payload(json.dumps({'base_url':address}),Path.cwd(),set())
                self.assertNotIn('SECRET',str(failure.exception))
        with self.assertRaises(PayloadValidationError):
            validate_json_payload(b'{broken',Path.cwd(),set())

class FakeStore:
    def __init__(self):
        self.users={'alice':uuid.uuid4(),'bob':uuid.uuid4()};self.sessions={};self.projects={};self.shares={}
    def login(self,name,password,peer):
        if name not in self.users or password!='a sufficiently long password': return None
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        self.sessions[token]={'id':self.users[name],'username':name,'csrf_token':csrf}
        return token,csrf
    def session(self,token):return self.sessions.get(token)
    def logout(self,token):self.sessions.pop(token,None)
    def list_projects(self,owner):return [x for x in self.projects.values() if x['owner_id']==owner]
    def project(self,owner,pid):
        row=self.projects.get(str(pid));return row if row and row['owner_id']==owner else None
    def add_project(self,owner,pid,name):self.projects[str(pid)]=dict(id=pid,owner_id=owner,name=name,created_at=datetime.now(timezone.utc))
    def add_share(self,owner,pid,key,version,checksum,expires=None):
        sid,token=uuid.uuid4(),secrets.token_urlsafe(32)
        self.shares[digest(token)]=dict(id=sid,project_id=pid,corpus_key=key,version=version,checksum_sha256=checksum,active=True,expires_at=expires)
        return sid,token
    def share(self,token):
        row=self.shares.get(digest(token));return row if row and row['active'] and (row['expires_at'] is None or row['expires_at']>datetime.now(timezone.utc)) else None
    def list_shares(self,owner,pid):return [x for x in self.shares.values() if str(x['project_id'])==str(pid) and self.project(owner,pid)]
    def revoke_share(self,owner,sid):
        for row in self.shares.values():
            if str(row['id'])==str(sid) and self.project(owner,row['project_id']): row['active']=False;return True
        return False
class FakeSupervisor:
    def __init__(self):self.calls=0
    def ensure(self,pid):self.calls+=1;raise ValueError('worker stub')
    def close(self):pass
class Engine:
    def frequency(self,q,**kwargs):return {'Freq':{q:1}}
    def kwic(self,q,**kwargs):return {'Total':1,'Context':[{'Source':'example','Context':q}]}

class PortalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        template=self.root/'template';(template/'configs').mkdir(parents=True);(template/'corpus_registry').mkdir()
        (template/'configs/pipeline.json').write_text(json.dumps({'project':{'name':'template'}}))
        self.settings=PortalSettings(self.root/'storage',template)
        self.store=FakeStore();self.supervisor=FakeSupervisor()
        def loader(record):
            root=self.settings.project_root(record['project_id'])/'data/releases'
            catalog=ReleaseCatalog(root,allowed={(record['corpus_key'],record['version'])})
            return search_app(root,service=CorpusService(catalog,SearchEngineCache(loader=lambda _:Engine())))
        self.app=create_app(self.settings,self.store,self.supervisor,loader)
        self.alice=TestClient(self.app,raise_server_exceptions=False);self.bob=TestClient(self.app,raise_server_exceptions=False)
        self.a=self.login(self.alice,'alice');self.b=self.login(self.bob,'bob')
        r=self.alice.post('/_portal/api/projects',json={'name':'Alice project'},headers=self.a)
        self.assertEqual(r.status_code,201,r.text);self.pid=r.json()['id']
    def tearDown(self):self.temp.cleanup()
    def login(self,client,name):
        r=client.post('/_portal/api/login',json={'username':name,'password':'a sufficiently long password'})
        self.assertEqual(r.status_code,200);return {'x-csrf-token':r.json()['csrf_token']}
    def release(self,version='1.0',indexed=True):
        root=self.settings.project_root(self.pid)
        ws=create_workspace(root/'fixtures','chinese-plus','run-'+version)
        (ws.path/'corpus/text.txt').write_text('Doc example\n#Global 领域=测试\n#Group\nItem: 电池。\n',encoding='gb18030')
        if indexed:(ws.path/'index/Org.dat').write_bytes(b'fixture')
        return create_release(ws.path,root/'data/releases',corpus_id='chinese-plus',version=version,corpus_format_version='1',pipeline_version='1',search_engine_version='LangSC.BCC 2.0.7')
    def test_login_cookie_has_no_deadline_and_logout_revokes_session(self):
        response=self.alice.post('/_portal/api/login',json={'username':'alice','password':'a sufficiently long password'})
        cookie=response.headers['set-cookie'].lower()
        self.assertNotIn('max-age=',cookie)
        self.assertNotIn('expires=',cookie)
        self.assertIn('httponly',cookie)
        self.assertEqual(self.alice.get('/_portal/api/me').status_code,200)
        self.assertEqual(self.alice.post('/_portal/api/logout',json={},headers={'x-csrf-token':response.json()['csrf_token']}).status_code,200)
        self.assertEqual(self.alice.get('/_portal/api/me').status_code,401)
    def test_project_proxy_timeout_reports_uncertain_task_status(self):
        import httpx
        from types import SimpleNamespace
        client=MagicMock();client.request=AsyncMock(side_effect=httpx.ReadTimeout('test timeout'))
        with patch.object(self.supervisor,'ensure',return_value=SimpleNamespace(port=9000,token='test-internal-token')),patch('corpus_portal.app.httpx.AsyncClient') as factory:
            factory.return_value.__aenter__.return_value=client
            response=self.alice.get(f'/p/{self.pid}/api/status')
        self.assertEqual(response.status_code,504)
        self.assertIn('可能已被后台接受',response.json()['detail'])
        self.assertIn('不要重复提交',response.json()['detail'])
    def share(self):
        r=self.alice.post(f'/_portal/api/projects/{self.pid}/shares',json={'version':'1.0'},headers=self.a)
        self.assertEqual(r.status_code,201,r.text);return r.json()
    def test_duplicate_name_requires_rename_and_does_not_create_folder(self):
        before=list((self.settings.storage/'projects').iterdir())
        response=self.alice.post('/_portal/api/projects',json={'name':'  ＡＬＩＣＥ   project  '},headers=self.a)
        self.assertEqual(response.status_code,409,response.text)
        self.assertIn('重新命名',response.json()['detail'])
        self.assertEqual(list((self.settings.storage/'projects').iterdir()),before)
        response=self.bob.post('/_portal/api/projects',json={'name':'Alice project'},headers=self.b)
        self.assertEqual(response.status_code,201,response.text)

    def test_account_and_project_isolation(self):
        self.assertEqual(len(self.alice.get('/_portal/api/projects').json()['items']),1)
        self.assertEqual(self.bob.get('/_portal/api/projects').json()['items'],[])
        for path in [f'/p/{self.pid}/',f'/_portal/api/projects/{self.pid}/versions',f'/_portal/api/projects/{self.pid}/shares']:
            self.assertEqual(self.bob.get(path).status_code,404)
        self.assertEqual(self.supervisor.calls,0)
        self.assertEqual(TestClient(self.app).get(f'/p/{self.pid}/').status_code,401)
    def test_csrf_and_origin(self):
        self.assertEqual(self.alice.post('/_portal/api/projects',json={'name':'x'}).status_code,403)
        self.assertEqual(self.alice.post('/_portal/api/projects',json={'name':'x'},headers={**self.a,'origin':'https://other.example'}).status_code,403)
        self.assertEqual(len(self.store.projects),1)
    def test_cookies_and_logout(self):
        result=self.bob.post('/_portal/api/login',json={'username':'bob','password':'a sufficiently long password'})
        cookie=result.headers['set-cookie'].lower()
        self.assertIn('httponly',cookie);self.assertIn('samesite=lax',cookie)
        csrf=result.json()['csrf_token']
        self.assertEqual(self.bob.post('/_portal/api/logout',json={},headers={'x-csrf-token':csrf}).status_code,200)
        self.assertEqual(self.bob.get('/_portal/api/me').status_code,401)
    def test_share_has_only_one_version_and_no_build_routes(self):
        self.release();self.release('2.0');share=self.share();base=share['url'].split(self.settings.public_url)[1].removesuffix('/search')
        guest=TestClient(self.app,raise_server_exceptions=False)
        corpora=guest.get(base+'/api/v1/corpora');self.assertEqual(corpora.status_code,200,corpora.text)
        cid=corpora.json()['items'][0]['id']
        versions=guest.get(base+'/api/v1/corpora/'+cid+'/versions').json()['items']
        self.assertEqual([x['version'] for x in versions],['1.0'])
        self.assertEqual(guest.get(base+'/api/v1/search?q=电池&version=2.0').status_code,404)
        self.assertEqual(guest.get(base+'/api/v1/search?q=电池').status_code,200)
        for path in ['api/upload','api/download','api/workflow/bootstrap','api/v1/exports/structured']:
            self.assertEqual(guest.get(base+'/'+path).status_code,404)
            self.assertIn(guest.post(base+'/'+path,json={}).status_code,[404,405])
        self.assertNotIn('语料库构建</a>',guest.get(base+'/search').text)
    def test_revocation_applies_after_cached_search(self):
        self.release();share=self.share();guest=TestClient(self.app)
        self.assertEqual(guest.get(share['url']).status_code,200)
        sid=share['id']
        self.assertEqual(self.bob.post(f'/_portal/api/shares/{sid}/revoke',json={},headers=self.b).status_code,404)
        self.assertEqual(self.alice.post(f'/_portal/api/shares/{sid}/revoke',json={},headers=self.a).status_code,200)
        self.assertEqual(guest.get(share['url']).status_code,404)
    def test_cannot_share_unindexed_release(self):
        self.release(indexed=False)
        self.assertEqual(self.alice.post(f'/_portal/api/projects/{self.pid}/shares',json={'version':'1.0'},headers=self.a).status_code,409)
    def test_clean_template_and_path_validation(self):
        root=self.settings.project_root(self.pid)
        self.assertEqual(list((root/'data/releases').iterdir()),[])
        with self.assertRaises(ValueError): verify_payload({'source_path':str(self.root/'private.jsonl')},root,set())
        with self.assertRaises(ValueError): verify_payload({'model_id':'../../other'},root,set())
        with self.assertRaises(ValueError): verify_payload({'python_executable':'another-program.exe'},root,set())
        verify_payload({'source_path':'data/raw/own.jsonl'},root,set())
        verify_payload({'base_url':'https://api.openai.com/v1'},root,set())
        with self.assertRaises(ValueError): verify_payload({'base_url':'http://127.0.0.1:8002'},root,set())
        with self.assertRaises(ValueError): verify_payload({'topic_table_path':str(self.root/'private.csv')},root,set())
        with self.assertRaises(ValueError): initialize_workspace(self.settings,self.pid,'overwrite')
    def test_scoped_browser_urls_and_csrf(self):
        source=b'''<html><head></head><body><a href="/search">Search</a><script>fetch('/api/bootstrap');fetch(`/api/status`);const url='https://api.example/v1';</script></body></html>'''
        result=rewrite_response(source,'text/html','/p/project','csrf-test').decode()
        self.assertIn("fetch('/p/project/api/bootstrap')",result)
        self.assertIn('href="/p/project/search"',result)
        self.assertIn('https://api.example/v1',result)
        self.assertIn("X-CSRF-Token",result)
        self.assertIn('Storage.prototype[method]',result)
        self.assertIn('/p/project::',result)
    def test_import_keeps_source_and_refuses_overwrite(self):
        from corpus_portal.imports import import_release
        source=self.release()
        source=source.path if hasattr(source,'path') else source
        other=self.alice.post('/_portal/api/projects',json={'name':'import target'},headers=self.a).json()['id']
        imported=import_release(self.settings,other,source)
        self.assertTrue((imported/'manifest.json').is_file())
        self.assertTrue((source/'manifest.json').is_file())
        self.assertEqual((imported/'corpus/text.txt').read_bytes(),(source/'corpus/text.txt').read_bytes())
        with self.assertRaises(ValueError): import_release(self.settings,other,source)
    def test_sharing_is_inside_original_publish_step(self):
        from corpus_portal.workspaces import ROOT
        original=(ROOT/'corpus_api/web/index.html').read_bytes()
        prefix='/p/'+self.pid
        rendered=rewrite_response(original,'text/html',prefix,'csrf-test').decode()
        import re
        publish=re.search(r'<section id="publish"[^>]*>(.*?)</section>',rendered,re.S).group(1)
        self.assertIn('portalShareButton',publish)
        self.assertIn('portalShareFrame',rendered)
        self.assertIn("frame.src='/_portal?embedded=1&share='",rendered)
        self.assertIn('event.source===frame.contentWindow',rendered)
        unshared=rewrite_response(original,'text/html','/share/example').decode()
        self.assertNotIn('portalShareButton',unshared)
    def test_first_start_prompts_once_and_never_overwrites_accounts(self):
        from corpus_portal.cli import setup_accounts
        store=MagicMock();store.has_users.return_value=False
        with patch('sys.stdin.isatty',return_value=True),patch('builtins.input',return_value='firstuser'),patch('getpass.getpass',side_effect=['long new password','long new password']),patch('builtins.print'):
            setup_accounts(store)
        store.add_user.assert_called_once_with('firstuser','long new password')
        store.has_users.return_value=True
        with patch('builtins.input') as ask,patch('getpass.getpass') as password,patch('builtins.print'):
            setup_accounts(store)
            ask.assert_not_called();password.assert_not_called()
        self.assertEqual(store.add_user.call_count,1)
    def test_noninteractive_first_setup_does_not_create_a_default_password(self):
        from corpus_portal.cli import setup_accounts
        store=MagicMock();store.has_users.return_value=False
        with patch('sys.stdin.isatty',return_value=False),patch('builtins.print'):
            with self.assertRaises(ValueError): setup_accounts(store)
        store.add_user.assert_not_called()
    def test_server_recommended_topic_interpreter_is_allowed(self):
        from corpus_portal.worker import allowed_python_executables
        recommended=str((self.root/'server-environment/python.exe').resolve())
        with patch('corpus_api.topic_workflow._recommended_python',return_value=recommended):
            allowed=allowed_python_executables()
        verify_payload({'python_executable':recommended},self.settings.project_root(self.pid),allowed)
        with self.assertRaises(ValueError):
            verify_payload({'python_executable':str(self.root/'another-program.exe')},self.settings.project_root(self.pid),allowed)
    def test_password_hash(self):
        encoded=password_hash('a sufficiently long password')
        self.assertNotIn('a sufficiently',encoded)
        self.assertTrue(password_matches('a sufficiently long password',encoded))
        self.assertFalse(password_matches('wrong',encoded))
        short=password_hash('123')
        self.assertTrue(password_matches('123',short))
        with self.assertRaises(ValueError): password_hash('12')
    def test_database_schema_connection_is_scoped(self):
        from corpus_registry.db import connect
        psycopg=MagicMock();setting=MagicMock();setting.connection_kwargs.return_value={'host':'example'}
        with patch('corpus_registry.db._psycopg',return_value=(psycopg,object())):
            with patch.dict('os.environ',{'CORPUS_DB_SCHEMA':'p_'+'a'*32}):
                with connect(setting):pass
            self.assertEqual(psycopg.connect.call_args.kwargs['options'],'-c search_path=p_'+'a'*32)
            with patch.dict('os.environ',{'CORPUS_DB_SCHEMA':'public;drop schema public'}):
                with self.assertRaises(ValueError):
                    with connect(setting):pass

if __name__=='__main__':unittest.main()
