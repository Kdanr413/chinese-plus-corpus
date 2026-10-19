import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from corpus_api import step_models as sm,recognition as api
from corpus_recognition.runtime import RecognitionCoordinator,RecognitionRunOptions
from corpus_recognition import AggregationStrategy

def config():
    data=sm.defaults()
    data['models']=[{'id':n,'model':'model-'+n,'model_version':'v1','base_url':'https://api.example.org/v1','api_key':'dummy-'+n,'weight':1.0} for n in ['clean','term','cross']]
    for s,n in [('cleaning','clean'),('term','term'),('cross','cross')]:
        data['steps'][s].update(mode='models',model_ids=[n])
    return data
PROMPT={'system_prompt':'role','task_instruction':'task'}

class StepModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_persistence_and_test_fingerprints_are_step_specific(self):
        data=sm.save_settings(self.root,config())
        for s in sm.STAGES:self.assertTrue(sm.mark_verified(self.root,s,sm.selected_models(data,s),PROMPT))
        self.assertEqual(sm.read_settings(self.root)['models'],data['models'])
        data['models'][2]['api_key']='changed-cross-key';sm.save_settings(self.root,data)
        self.assertTrue(sm.verified(self.root,'term',PROMPT))
        self.assertTrue(sm.verified(self.root,'cleaning',PROMPT))
        self.assertFalse(sm.verified(self.root,'cross',PROMPT))
        self.assertFalse(sm.verified(self.root,'term',{**PROMPT,'task_instruction':'changed'}))
    def test_wrong_model_and_stale_inflight_test_cannot_mark_verified(self):
        data=sm.save_settings(self.root,config());before=sm.fingerprint(data,'term',PROMPT)
        self.assertFalse(sm.mark_verified(self.root,'term',sm.selected_models(data,'cross'),PROMPT))
        data['steps']['term']['strategy']='weighted';sm.save_settings(self.root,data)
        self.assertFalse(sm.mark_verified(self.root,'term',sm.selected_models(data,'term'),PROMPT,before))
    def test_missing_cleaning_choice_and_excess_model_minimum_are_rejected(self):
        data=config();data['steps']['cleaning']['model_ids']=['clean','term']
        with self.assertRaisesRegex(ValueError,'一个模型'):sm.save_settings(self.root,data)
        data=config();data['steps']['cross']['min_validators']=2
        with self.assertRaisesRegex(ValueError,'数量'):sm.save_settings(self.root,data)
    def test_cross_providers_use_cross_group_and_rule_mode_uses_no_term_model(self):
        data=config();options=RecognitionRunOptions(use_external_model=True,validator_models=tuple([data['models'][1]]),include_builtin_rules=False,cross_use_external_model=True,cross_validator_models=tuple([data['models'][2]]),cross_strategy=AggregationStrategy.WEIGHTED)
        cross=RecognitionCoordinator._cross_options(options)
        self.assertEqual(cross.validator_models[0].id,'cross');self.assertEqual(cross.strategy,AggregationStrategy.WEIGHTED)
        coordinator=RecognitionCoordinator.__new__(RecognitionCoordinator);coordinator.prompts={'term':None,'topic':None,'cross':None}
        with patch('corpus_recognition.runtime.OpenAICompatibleValidatorProvider') as provider:
            coordinator._providers(cross,'cross');self.assertEqual(provider.call_args.args[0].validator_id,'cross')
        options=RecognitionRunOptions(use_external_model=True,validator_models=tuple([data['models'][1]]),include_builtin_rules=False,cross_use_external_model=False)
        cross=RecognitionCoordinator._cross_options(options)
        with patch('corpus_recognition.runtime.OpenAICompatibleValidatorProvider') as provider,patch('corpus_recognition.runtime.BuiltinRuleValidatorProvider') as builtin:
            coordinator._providers(cross,'cross');provider.assert_not_called();builtin.assert_called_once()
    def test_legacy_cross_configuration_remains_compatible(self):
        options=RecognitionRunOptions();self.assertIs(RecognitionCoordinator._cross_options(options),options)
    def test_api_persists_selection_and_only_matching_success_marks_it_tested(self):
        app=FastAPI();app.include_router(api.router)
        with patch.object(api.legacy,'PROJECT_ROOT',self.root),patch.object(api,'_formal_step_prompt',return_value=PROMPT),patch.object(api,'require_admin') as guard,patch.object(api.STATE,'running',False):
            client=TestClient(app)
            data=config()
            response=client.put('/api/v1/recognition/step-settings',json={k:v for k,v in data.items() if k!='_tests'})
            self.assertEqual(response.status_code,200,response.text)
            selected=sm.selected_models(data,'cross')
            payload={'sentence':'test','models':selected,'strategy':'majority',**PROMPT}
            with patch.object(api,'_execute_stage_prompt',return_value={'schema_valid':True}):
                result=client.post('/api/v1/recognition/prompt-stages/cross/test',json=payload)
            self.assertTrue(result.json()['saved_configuration_verified'])
            self.assertTrue(client.get('/api/v1/recognition/step-settings').json()['tested']['cross'])
            data['steps']['cross']['strategy']='weighted'
            client.put('/api/v1/recognition/step-settings',json={k:v for k,v in data.items() if k!='_tests'})
            self.assertFalse(client.get('/api/v1/recognition/step-settings').json()['tested']['cross'])
            with patch.object(api,'_execute_stage_prompt',return_value={'schema_valid':False}):
                result=client.post('/api/v1/recognition/prompt-stages/cross/test',json={**payload,'strategy':'weighted'})
            self.assertFalse(result.json()['saved_configuration_verified']);guard.assert_called()
    def test_formal_run_rejects_untested_cross_model_even_without_term_model(self):
        app=FastAPI();app.include_router(api.router);data=config();data['steps']['term'].update(mode='rules',model_ids=[]);sm.save_settings(self.root,data)
        with patch.object(api.legacy,'PROJECT_ROOT',self.root),patch.object(api,'_formal_step_prompt',return_value=PROMPT),patch.object(api,'require_admin'),patch.object(api.STATE,'running',False),patch.object(api.threading,'Thread') as thread:
            payload={'human_validation':False,'use_external_model':False,'cross_validation_enabled':True,'cross_use_external_model':True,'cross_validator_models':sm.selected_models(data,'cross'),'cross_strategy':'majority','topic_table_path':'reference.json'}
            client=TestClient(app);result=client.post('/api/v1/recognition/runs',json=payload)
            self.assertEqual(result.status_code,400);thread.assert_not_called()
            sm.mark_verified(self.root,'cross',sm.selected_models(data,'cross'),PROMPT)
            result=client.post('/api/v1/recognition/runs',json=payload);self.assertEqual(result.status_code,202,result.text)
            self.assertEqual(thread.call_args.kwargs['args'][0].cross_validator_models[0].id,'cross')

    def test_cleaning_http_error_reports_status_and_sanitized_endpoint(self):
        import urllib.error
        app=FastAPI();app.include_router(api.router)
        payload={'sentence':'测试句子','model_name':'example','base_url':'https://api.example.org/llm','api_key':'dummy-test-key'}
        error=urllib.error.HTTPError('https://user:private@api.example.org/llm/chat/completions?token=private',404,'Not Found',{},None)
        with patch.object(api.legacy,'PROJECT_ROOT',self.root),patch.object(api,'_formal_step_prompt',return_value=PROMPT),patch.object(api.legacy,'execute_prompt_test',side_effect=error),patch.object(api,'require_admin'):
            r=TestClient(app).post('/api/v1/recognition/prompt-stages/cleaning/test',json=payload)
        self.assertEqual(r.status_code,502);self.assertEqual(r.json()['upstream_status'],404);self.assertEqual(r.json()['request_url'],'https://api.example.org/llm/chat/completions');self.assertNotIn('private',r.text);self.assertNotIn('dummy-test-key',r.text)
    def test_cleaning_timeout_has_retryable_response_instead_of_opaque_500(self):
        app=FastAPI();app.include_router(api.router)
        with patch.object(api.legacy,'PROJECT_ROOT',self.root),patch.object(api,'_formal_step_prompt',return_value=PROMPT),patch.object(api.legacy,'execute_prompt_test',side_effect=TimeoutError('private internal details')),patch.object(api,'require_admin'):
            r=TestClient(app).post('/api/v1/recognition/prompt-stages/cleaning/test',json={'sentence':'测试','model_name':'example','base_url':'https://api.example.org/v1','api_key':'dummy-test-key'})
        self.assertEqual(r.status_code,504);self.assertNotIn('private',r.text)
