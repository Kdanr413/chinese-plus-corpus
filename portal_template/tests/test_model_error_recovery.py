import json,os,tempfile,unittest,shutil
from pathlib import Path
from unittest.mock import patch
from scripts.workflow.model_errors import ModelErrors
from scripts.workflow.file_paths import io_path
from scripts.workflow import run_validation_llm as runner

class ErrorRecoveryTests(unittest.TestCase):
    def test_replay_api_requires_auth_and_prevents_duplicate_submission(self):
        from starlette.requests import Request
        from corpus_api import model_errors as api
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'CORPUS_PROJECT_ROOT':tmp,'ADMIN_API_TOKEN':'admin'}):
            store=ModelErrors(tmp);key=store.record('term','https://api.example/v1/chat/completions',{'model':'m'},TimeoutError('timeout'))
            request=Request({'type':'http','headers':[(b'x-admin-token',b'admin')]})
            invalid=Request({'type':'http','headers':[]})
            with self.assertRaises(PermissionError):api.failures(invalid,page=1,page_size=100)
            self.assertEqual(api.failures(request,page=1,page_size=100)['items'][0]['id'],key)
            with patch.object(api.threading,'Thread') as thread:
                body=api.ReplayRequest(ids=[key],api_key='private')
                self.assertEqual(api.replay(body,request)['accepted'],[key])
                self.assertEqual(api.replay(body,request)['accepted'],[])
                self.assertEqual(thread.call_count,1)

    def test_sentence_extraction_failure_does_not_reject_or_stop_other_sentences(self):
        from corpus_recognition.term_intersection import SentenceTermIntersectionProvider
        from corpus_recognition.providers import ResolvedValidatorSettings
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from corpus_recognition.models import Document
        transport=MagicMock();transport.complete.side_effect=[TimeoutError('timeout'),'{"terms":["电池"]}']
        docs=[Document(document_id='a',source='s',raw_text='电池。',clean_text='电池。',processing_version='1'),Document(document_id='b',source='s',raw_text='电池。',clean_text='电池。',processing_version='1')]
        settings=ResolvedValidatorSettings(validator_id='m',provider='compatible',model='m',model_version='1',base_url='https://api.example/v1',api_key='key')
        prompt=SimpleNamespace(template=SimpleNamespace(system_prompt='role',task_instruction='terms'))
        with patch.dict(os.environ,{'CORPUS_PROJECT_ROOT':''}):provider=SentenceTermIntersectionProvider(settings,prompt,docs,transport=transport)
        self.assertIn('a',provider.failed_documents)
        self.assertIn('电池',provider.terms_by_document['b'])
    def test_failed_request_is_saved_without_key_and_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ModelErrors(tmp)
            key=store.record('cleaning','https://api.example/v1/chat/completions',{'model':'m','messages':[]},TimeoutError('SECRET timed out'),'SECRET')
            self.assertNotIn('SECRET',json.dumps(store.read(key)))
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self):return b'{"choices":[{"message":{"content":"{\\"decision\\":\\"keep\\"}"}}]}'
            with patch.dict(os.environ,{'CORPUS_PROJECT_ROOT':tmp,'CORPUS_DB_SCHEMA':'public'}),patch('scripts.workflow.model_errors.urllib.request.urlopen',return_value=Response()):
                result=store.retry(key,'NEW-KEY')
            self.assertEqual(result['status'],'replayed')
            self.assertEqual(result['result']['decision'],'keep')
            self.assertNotIn('NEW-KEY',json.dumps(store.read(key)))

    def test_continue_after_one_failed_cleaning_item(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tasks=root/'tasks.jsonl';responses=root/'responses.jsonl'
            tasks.write_text(json.dumps({'original_text':'one'})+'\n'+json.dumps({'original_text':'two'}),encoding='utf-8')
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self):return b'{"choices":[{"message":{"content":"{\\"decision\\":\\"keep\\"}"}}]}'
            args=['runner','--mode','cleaning','--tasks',str(tasks),'--responses',str(responses),'--limit','0','--max-attempts','1','--require-complete','--continue-on-errors','--cache-root',str(root/'cache')]
            with patch.object(sys,'argv',args),patch.object(runner,'PROJECT_ROOT',root),patch.dict(os.environ,{'CLEANING_LLM_API_KEY':'key','CLEANING_LLM_MODEL':'m'}),patch.object(runner.urllib.request,'urlopen',side_effect=[TimeoutError('timeout'),Response()]):
                self.assertEqual(runner.main(),0)
            rows=[json.loads(line) for line in responses.read_text().splitlines()]
            self.assertEqual([row['status'] for row in rows],['error','success'])
            self.assertTrue(rows[0]['failure_id'])

    @unittest.skipUnless(os.name=='nt','Windows native long path test')
    def test_actual_windows_write_beyond_280_characters(self):
        from scripts.prepare import clean
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/('a'*110)/('b'*110)/'clean_text_by_source'
            doc=dict(doc_id='doc_unique_0001',title='完整标题',source_type='测试来源',source='测试文档',domain='',text_genre='',genre_confidence=1,topics_auto=[],quality='完整可用',paragraph_count=1,section_count=1,sentence_count=1,original_path='data/raw/original.txt',text='完整正文。')
            try:
                with patch.object(clean,'OUT_CLEAN_TEXT_DIR',output):path=Path(clean.write_clean_source_text(doc))
                self.assertGreater(len(str(path)),280)
                self.assertIn('完整正文',io_path(path).read_text(encoding='utf-8'))
            finally:
                safe=Path(tmp).resolve()
                self.assertTrue(safe.is_relative_to(Path(tempfile.gettempdir()).resolve()))
                shutil.rmtree(io_path(safe))
