import json,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from scripts.workflow.topic_reference import remove_reference
from scripts.ui import server
from corpus_topic_workflow import WorkflowManager

class TopicReferenceTests(unittest.TestCase):
 def test_remove_archive_and_upload_another_domain(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);config=root/"configs/pipeline.json";config.parent.mkdir()
   old=root/"configs/topics/old.json";old.parent.mkdir();old.write_text('{"original":true}')
   config.write_text(json.dumps({"topics":{"reference_taxonomy":"configs/topics/old.json"}}))
   history=root/"data/releases/history.txt";history.parent.mkdir(parents=True);history.write_text("keep")
   result=remove_reference(root);self.assertFalse(old.exists());self.assertEqual(history.read_text(encoding="utf-8"),"keep")
   self.assertEqual((root/result["archive_path"]/"reference_taxonomy.json").read_text(encoding="utf-8"),'{"original":true}')
   new=root/"configs/topics/reference_taxonomy.json";self.assertEqual(json.loads(new.read_text(encoding="utf-8")),{})
   payload={"taxonomy_version":"1","classification_object":"医学","root":{"id":"M","name":"医学","children":[]}}
   with patch.object(server,"PROJECT_ROOT",root),patch.object(server,"DEFAULT_CONFIG",config),patch.dict(server.UPLOAD_TARGETS,{"topic_taxonomy":new}):
    server.Handler.save_upload(None,"topic_taxonomy","医学.json",json.dumps(payload).encode())
   self.assertEqual(json.loads(new.read_text(encoding="utf-8"))["root"]["name"],"医学")
 def test_protected_raw_reference_cannot_be_removed(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/"configs").mkdir();(root/"configs/pipeline.json").write_text(json.dumps({"topics":{"reference_taxonomy":"data/raw/a.json"}}))
   with self.assertRaises(ValueError):remove_reference(root)
 def test_new_round_uses_uploaded_reference_and_clear_is_empty(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);package=root/"corpus_topic_workflow";shutil.copytree(Path(__file__).resolve().parents[1]/"corpus_topic_workflow/configs",package/"configs")
   (root/"configs/topics").mkdir(parents=True);(root/"configs/pipeline.json").write_text(json.dumps({"topics":{"reference_taxonomy":"configs/topics/reference_taxonomy.json"}}))
   ref={"taxonomy_version":"1","classification_object":"医学","root":{"id":"M","name":"医学","children":[{"id":"M1","name":"诊断","children":[{"id":"M2","name":"影像","children":[]}]}]}}
   (root/"configs/topics/reference_taxonomy.json").write_text(json.dumps(ref))
   source=root/"sentences.jsonl";source.write_text("\n".join(json.dumps({"doc_id":"d","sentence_id":str(i),"text":"影像检查是医学诊断的重要方法之一。"}) for i in range(6)))
   manager=WorkflowManager(package,root/"results",root/"canonical.json")
   round1=manager.create_round(source,sample_size=3);path=manager.round_dir(round1["round_id"])
   self.assertEqual(json.loads((path/"reference_taxonomy.json").read_text(encoding="utf-8")),ref)
   self.assertTrue(any("医学" in x["path"] for x in json.loads((path/"baseline_taxonomy.json").read_text(encoding="utf-8"))["topics"]))
   original=(path/"reference_taxonomy.json").read_bytes();remove_reference(root)
   round2=manager.create_round(source,sample_size=3)
   self.assertEqual(json.loads((manager.round_dir(round2["round_id"])/"baseline_taxonomy.json").read_text(encoding="utf-8"))["topics"],[])
   self.assertEqual((path/"reference_taxonomy.json").read_bytes(),original)

 def test_export_upload_and_round_baseline(self):
  from corpus_topic_workflow.run_topic_demo import flatten_taxonomy
  from scripts.topics.taxonomy_workflow import flatten_reference
  # Deliberately child-first, with two independent top-level branches.
  rows=[{"id":"leaf","name":"检查","path":"医学 > 诊断 > 检查","parent_id":"middle","definition":"检查定义","keywords":["影像"],"status":"active"},
        {"id":"other","name":"教育","path":"教育","parent_id":"","definition":"教育定义","keywords":[]},
        {"id":"middle","name":"诊断","path":"医学 > 诊断","parent_id":"root","keywords":[]},
        {"id":"root","name":"医学","path":"医学","parent_id":"","definition":"医学定义","keywords":[]}]
  ref={"taxonomy_name":"复用体系","taxonomy_version":"3","nodes":rows}
  nodes,errors=flatten_reference(ref);self.assertEqual(errors,[]);self.assertEqual(len(nodes),4)
  self.assertEqual(flatten_taxonomy(ref),nodes)
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);package=root/"corpus_topic_workflow";shutil.copytree(Path(__file__).resolve().parents[1]/"corpus_topic_workflow/configs",package/"configs")
   config=root/"configs/pipeline.json";config.parent.mkdir();config.write_text(json.dumps({"topics":{"reference_taxonomy":"configs/topics/reference_taxonomy.json"}}))
   target=root/"configs/topics/reference_taxonomy.json"
   original=json.dumps(ref,ensure_ascii=False).encode("utf-8")
   with patch.object(server,"PROJECT_ROOT",root),patch.object(server,"DEFAULT_CONFIG",config),patch.dict(server.UPLOAD_TARGETS,{"topic_taxonomy":target}):
    result=server.Handler.save_upload(None,"topic_taxonomy","taxonomy.json",original)
   self.assertTrue(result["ok"])
   uploaded=json.loads(target.read_text(encoding="utf-8"));self.assertEqual(uploaded["nodes"],rows)
   source=root/"sentences.jsonl";source.write_text("\n".join(json.dumps({"doc_id":"d","sentence_id":str(i),"text":"影像检查是医学诊断的重要方法之一。"}) for i in range(6)),encoding="utf-8")
   manager=WorkflowManager(package,root/"results",root/"canonical.json");rd=manager.create_round(source,sample_size=3)
   state=json.loads((manager.round_dir(rd["round_id"])/"baseline_taxonomy.json").read_text(encoding="utf-8"))
   by_path={x["path"]:x for x in state["topics"]}
   self.assertEqual(set(by_path),{x["path"] for x in rows})
   self.assertEqual(by_path["医学 > 诊断 > 检查"]["keywords"],["影像"])
   self.assertEqual(by_path["医学"]["definition"],"医学定义")

 def test_export_invalid_hierarchy_is_rejected(self):
  from scripts.topics.taxonomy_workflow import flatten_reference
  for rows in ([],[{"id":"a","name":"B","path":"A > B","parent_id":"missing"}],
               [{"id":"a","name":"A","path":"A"},{"id":"a","name":"B","path":"B"}],
               [{"id":"a","name":"A","path":"A","keywords":"wrong"}]):
   self.assertTrue(flatten_reference({"nodes":rows})[1])

 def test_reuse_approved_reference_prepares_classification_without_clustering(self):
  from corpus_api import topic_workflow as api
  import hashlib
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);package=root/"corpus_topic_workflow";shutil.copytree(Path(__file__).resolve().parents[1]/"corpus_topic_workflow/configs",package/"configs")
   config=root/"configs/pipeline.json";config.parent.mkdir();config.write_text(json.dumps({"topics":{"reference_taxonomy":"configs/topics/reference_taxonomy.json"}}),encoding="utf-8")
   reference={"taxonomy_name":"正式体系","taxonomy_version":"3","status":"approved","test_only":False,"nodes":[{"id":"one","name":"医学","path":"医学","parent_id":""},{"id":"two","name":"诊断","path":"医学 > 诊断","parent_id":"one"},{"id":"three","name":"影像检查","path":"医学 > 诊断 > 影像检查","parent_id":"two","keywords":["影像检查"]}]}
   path=root/"configs/topics/reference_taxonomy.json";path.parent.mkdir();path.write_text(json.dumps(reference),encoding="utf-8")
   source=root/"data/processed/corpus/jsonl/sentences.jsonl";source.parent.mkdir(parents=True);source.write_text("\n".join(json.dumps({"doc_id":"d","sentence_id":str(i),"text":"影像检查是医学诊断的重要方法之一。"}) for i in range(6)),encoding="utf-8")
   manager=WorkflowManager(package,root/"workflows",root/"canonical.json")
   active=root/"data/curated/topics/taxonomy.json";active.parent.mkdir(parents=True);active.write_text('{"test_only":true}',encoding="utf-8")
   profile={"source_unit_count":6,"sample_size":3,"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}
   with patch.object(api,"PROJECT_ROOT",root),patch.object(api,"corpus_data_root",return_value=root/"data"),patch.object(api,"workflow_manager",return_value=manager),patch.object(api,"_source_profile",return_value=profile),patch.object(api,"_paths",return_value=(root/"workflows",root/"canonical.json",active)):
    result=api.reuse_approved_reference()
    self.assertTrue(result["ready_for_full_classification"]);self.assertTrue(result["round"]["published"])
    state=json.loads(active.read_text(encoding="utf-8"));self.assertEqual(state["taxonomy_version"],"3");self.assertFalse(state["test_only"])
    self.assertIn("影像检查",[node["name"] for node in state["nodes"]])
    self.assertTrue(list((active.parent/"history").glob('*.json')))
    self.assertTrue(api.reuse_approved_reference()["reused"])
    reference["test_only"]=True;path.write_text(json.dumps(reference),encoding="utf-8")
    with self.assertRaises(ValueError):api.reuse_approved_reference()
