import json
import tempfile
import unittest
from pathlib import Path
from scripts.workflow.model_cache import AnnotationCache
from scripts.workflow.prompting import build_messages

TEMPLATE={'system_prompt':'role','task_instruction':'clean','output_schema':'json'}

class ModelCacheTests(unittest.TestCase):
    def test_repeated_sentence_with_new_id_reuses_and_rebinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=AnnotationCache(tmp)
            old={'paragraph_id':'old-id','original_text':'电池储能。','previous_context':'前文'}
            new={**old,'paragraph_id':'new-id'}
            key,binding=cache.identify('https://api.example/v1','m',build_messages(TEMPLATE,old))
            other,target=cache.identify('https://api.example/v1/chat/completions','m',build_messages(TEMPLATE,new))
            self.assertEqual(key,other)
            self.assertTrue(cache.put(key,binding,json.dumps({'paragraph_id':'old-id','decision':'keep'})))
            self.assertEqual(json.loads(cache.get(other,target))['paragraph_id'],'new-id')
            for changed in [{**old,'previous_context':'不同前文'},{**old,'original_text':'风力发电。'}]:
                self.assertNotEqual(key,cache.identify('https://api.example/v1','m',build_messages(TEMPLATE,changed))[0])
            self.assertNotEqual(key,cache.identify('https://api.example/v1','m2',build_messages(TEMPLATE,old))[0])
            self.assertNotEqual(key,cache.identify('https://api.example/v1','m',build_messages({**TEMPLATE,'task_instruction':'different'},old))[0])

    def test_project_isolation_bad_json_and_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            a=AnnotationCache(Path(tmp)/'a');b=AnnotationCache(Path(tmp)/'b')
            key,bindings=a.identify('https://api.example/v1','m',build_messages(TEMPLATE,{'sentence':'same'}))
            self.assertFalse(a.put(key,bindings,'not JSON'))
            self.assertIsNone(a.get(key,bindings))
            a.put(key,bindings,'{"terms":["电池"]}')
            self.assertIsNone(b.get(key,bindings))
            entry=json.loads(a.path(key).read_text());entry['content']='{"terms":[]}'
            a.path(key).write_text(json.dumps(entry))
            self.assertIsNone(a.get(key,bindings))

    def test_reference_taxonomy_and_version_affect_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=AnnotationCache(tmp)
            messages=[{'role':'user','content':'input with reference taxonomy A'}]
            key,_=cache.identify('https://api.example/v1','m',messages,'v1')
            self.assertNotEqual(key,cache.identify('https://api.example/v1','m',messages,'v2')[0])
            self.assertNotEqual(key,cache.identify('https://api.example/v1','m',[{'role':'user','content':'input with reference taxonomy B'}],'v1')[0])
