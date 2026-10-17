import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.workflow.core import WorkflowContext
from scripts.workflow.stages import _build_syntax_tree_index
from scripts.workflow.syntax_checkpoint import _load_cache


class SyntaxCheckpointTests(unittest.TestCase):
 def setup_project(self, root):
  output=root/"output";output.mkdir()
  config={"paths":{"index_output":"output"},"linguistic":{"jieba_user_dict":"dict.txt"},"index":{"encoding":"gb18030"},"syntax_tree":{"max_consecutive_failures":2}}
  (root/"config.json").write_text(json.dumps(config),encoding="utf-8")
  rows=[{"item_id":str(i),"doc_id":"d","doc_name":"示例","text":text,"global_pairs":["语种=汉语"],"group_pairs":[]} for i,text in enumerate(["甲句","乙句"])]
  (output/"index_items.jsonl").write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows),encoding="utf-8")
  return WorkflowContext(root,root/"config.json"),output

 def run_build(self,ctx,request):
  with patch("scripts.workflow.stages._active_terms",return_value=([],{})), patch("scripts.workflow.stages._create_jieba_tokenizer",return_value=None), patch("scripts.workflow.stages._segment_text_with_pos",side_effect=lambda text,t:([text],["n"])), patch("scripts.workflow.stages._request_syntax_tree",side_effect=request), patch("scripts.workflow.stages._emit_progress"):
   return _build_syntax_tree_index(ctx)

 def test_interruption_saves_success_and_resume_skips_it(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp));final=out/"corpus_index_syntax_tree.txt";final.write_bytes(b"previous final")
   calls=[]
   def interrupted(url,words,*args,**kwargs):
    calls.append(words)
    if len(calls)==2:raise KeyboardInterrupt()
    return "(ROOT "+words[0]+")",{"Units":"(ROOT 甲句)"}
   with self.assertRaises(KeyboardInterrupt):self.run_build(ctx,interrupted)
   self.assertEqual(final.read_bytes(),b"previous final")
   self.assertEqual(len((out/"syntax_tree_cache.jsonl").read_text(encoding="utf-8").splitlines()),1)
   self.assertEqual(json.loads((out/"syntax_tree_progress.json").read_text(encoding="utf-8"))["completed"],1)
   calls=[]
   def resumed(url,words,*args,**kwargs):
    calls.append(words);return "(ROOT "+words[0]+")",{}
   path,metrics,errors=self.run_build(ctx,resumed)
   self.assertEqual(calls,[["乙句"]]);self.assertEqual(metrics["syntax_cache_hits"],1)
   self.assertEqual(metrics["syntax_successes"],2);self.assertEqual(errors,[])
   self.assertIn("甲句",path.read_text(encoding="gb18030"))

 def test_failure_continues_then_retry_only_failed(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp))
   def request(url,words,*args,**kwargs):
    if words==["甲句"]:raise RuntimeError("service timeout")
    return "(ROOT 乙句)",{}
   _,metrics,errors=self.run_build(ctx,request)
   self.assertEqual(len(errors),1);self.assertEqual(metrics["syntax_successes"],1)
   self.assertIn("service timeout",(out/"syntax_tree_errors.csv").read_text(encoding="utf-8-sig"))
   seen=[]
   def retry(url,words,*args,**kwargs):seen.append(words);return "(ROOT 甲句)",{}
   _,metrics,errors=self.run_build(ctx,retry)
   self.assertEqual(seen,[["甲句"]]);self.assertEqual(metrics["syntax_cache_hits"],1);self.assertEqual(errors,[])

 def test_service_outage_stops_after_threshold(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp))
   with self.assertRaisesRegex(RuntimeError,"连续失败 2"):
    self.run_build(ctx,RuntimeError("offline"))
   self.assertFalse((out/"corpus_index_syntax_tree.txt").exists())
   self.assertEqual(json.loads((out/"syntax_tree_progress.json").read_text(encoding="utf-8"))["status"],"failed")
   self.assertEqual(len((out/"syntax_tree_responses.jsonl").read_text(encoding="utf-8").splitlines()),2)

 def test_cache_ignores_partial_record_and_endpoint_changes(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp))
   self.run_build(ctx,lambda *args,**kwargs:("(ROOT 示例)",{}))
   cache=out/"syntax_tree_cache.jsonl"
   with cache.open("ab") as handle:handle.write(b'{"broken":"\xe4')
   records,invalid=_load_cache(cache);self.assertEqual(len(records),2);self.assertEqual(invalid,1)
   seen=[]
   def changed(*args,**kwargs):seen.append(1);return "(ROOT 新服务)",{}
   with patch.dict("os.environ",{"CORPUS_SYNTAX_TREE_BASE_URL":"http://different.example"}):self.run_build(ctx,changed)
   self.assertEqual(len(seen),2)
   records,invalid=_load_cache(cache);self.assertEqual(len(records),4)

 def test_status_does_not_scan_results(self):
  from corpus_api import workbench
  with patch.object(workbench,"require_admin"),patch.object(workbench.legacy,"result_summary",side_effect=AssertionError("must not scan")):
   result=workbench.status(None)
   self.assertIn("job",result)

 def test_transient_progress_rename_denial_retries_without_losing_old_snapshot(self):
  from scripts.workflow.atomic_files import write_progress_json
  import os
  with tempfile.TemporaryDirectory() as temp:
   target=Path(temp)/"progress.json";target.write_text('{"completed":1}',encoding="utf-8")
   original=os.replace;attempts=[]
   def flaky(source,destination):
    attempts.append(str(source))
    if len(attempts)<3:
     self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["completed"],1)
     raise PermissionError(5,"Access denied")
    return original(source,destination)
   with patch("scripts.workflow.atomic_files.os.replace",side_effect=flaky),patch("scripts.workflow.atomic_files.time.sleep"):
    write_progress_json(target,{"completed":2})
   self.assertEqual(len(attempts),3)
   self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["completed"],2)
   self.assertEqual(list(Path(temp).glob('*.tmp')),[])

 def test_progress_permission_denial_does_not_abort_successful_processing(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp))
   with patch("scripts.workflow.syntax_checkpoint.write_progress_json",side_effect=PermissionError(5,"Access denied")):
    path,metrics,errors=self.run_build(ctx,lambda *args,**kwargs:("(ROOT 示例)",{}))
   self.assertTrue(path.exists());self.assertEqual(metrics["syntax_successes"],2);self.assertEqual(errors,[])
   self.assertEqual(len(_load_cache(out/"syntax_tree_cache.jsonl")[0]),2)

 def test_progress_denial_does_not_hide_original_service_failure(self):
  with tempfile.TemporaryDirectory() as temp:
   ctx,out=self.setup_project(Path(temp))
   with patch("scripts.workflow.syntax_checkpoint.write_progress_json",side_effect=PermissionError(5,"Access denied")):
    with self.assertRaisesRegex(RuntimeError,"连续失败 2"):
     self.run_build(ctx,RuntimeError("HTTP Error 502"))
   self.assertEqual(len((out/"syntax_tree_responses.jsonl").read_text(encoding="utf-8").splitlines()),2)

 @unittest.skipUnless(__import__("os").name=="nt","Windows file sharing semantics")
 def test_real_windows_reader_lock_is_retried(self):
  import ctypes,threading
  from ctypes import wintypes
  from scripts.workflow.atomic_files import write_progress_json
  kernel=ctypes.WinDLL("kernel32",use_last_error=True)
  kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p]
  kernel.CreateFileW.restype=wintypes.HANDLE
  kernel.CloseHandle.argtypes=[wintypes.HANDLE]
  with tempfile.TemporaryDirectory() as temp:
   target=Path(temp)/"progress.json";target.write_text('{"completed":1}',encoding="utf-8")
   handle=kernel.CreateFileW(str(target),0x80000000,3,None,3,128,None)
   self.assertNotEqual(handle,ctypes.c_void_p(-1).value)
   timer=threading.Timer(0.08,lambda:kernel.CloseHandle(handle));timer.start()
   try:write_progress_json(target,{"completed":2})
   finally:timer.join()
   self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["completed"],2)
