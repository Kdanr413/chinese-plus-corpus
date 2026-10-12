import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.workflow import run_validation_llm as runner


class ValidationRunnerTests(unittest.TestCase):
    def run_case(self, limit):
        with tempfile.TemporaryDirectory() as tmp:
            tasks=Path(tmp)/'tasks.jsonl'
            responses=Path(tmp)/'responses.jsonl'
            tasks.write_text('\n'.join(json.dumps({'original_text':'text'}) for _ in range(3)),encoding='utf-8')
            original=json.dumps({'task_index':0,'status':'success','parsed':{'decision':'keep'}})+'\n'
            responses.write_text(original,encoding='utf-8')
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self):return json.dumps({'choices':[{'message':{'content':'{"decision":"keep"}'}}]}).encode()
            argv=['runner','--mode','cleaning','--tasks',str(tasks),'--responses',str(responses),'--limit',str(limit),'--require-complete']
            with patch.object(sys,'argv',argv),patch.dict(os.environ,{'CLEANING_LLM_API_KEY':'dummy','CLEANING_LLM_MODEL':'dummy'}),patch.object(runner.urllib.request,'urlopen',return_value=Response()) as transport:
                result=runner.main()
            text=responses.read_text(encoding='utf-8')
            self.assertTrue(text.startswith(original))
            return result,transport.call_count,[json.loads(x)['task_index'] for x in text.splitlines()]

    def test_unlimited_resume_skips_success_and_preserves_results(self):
        self.assertEqual(self.run_case(0),(0,2,[0,1,2]))

    def test_explicit_limit_still_reports_remaining_tasks(self):
        self.assertEqual(self.run_case(1),(2,1,[0,1]))
