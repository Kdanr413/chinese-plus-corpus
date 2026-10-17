import json,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from corpus_topic_workflow.workflow_manager import WorkflowManager


class SavedRematchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.package=self.root/"corpus_topic_workflow";self.config=self.package/"configs";self.config.mkdir(parents=True)
  self.config.joinpath("topic_seed_terms.csv").write_text("term,topic_node_ids\n",encoding="utf-8")
  self.config.joinpath("bertopic.json").write_text(json.dumps({"matching":{"cluster_match_threshold":.46,"weights":{"vector":.35,"approved_terms":.15,"taxonomy_keywords":.45,"hierarchy":.05}}}),encoding="utf-8")
  self.round=self.root/"workflow/rounds/R0001";self.clustering=self.round/"clustering";self.clustering.mkdir(parents=True)
  self.state={"round_id":"R0001","status":"clustered","models":[],"published":False}
  self.write("round.json",self.state)
  self.write("reference_taxonomy.json",{"nodes":[{"id":"a","name":"能源","path":"能源","parent_id":"","keywords":[]},{"id":"b","name":"储能","path":"能源 > 储能","parent_id":"a","keywords":[]},{"id":"c","name":"储能材料","path":"能源 > 储能 > 储能材料","parent_id":"b","keywords":["电极材料"]}]})
  self.write("clustering/demo_topic_summary.json",{"topics":[{"cluster_id":"BT_TOPIC_001","unit_count":10,"document_count":2,"representative_words":[{"term":"电极材料"}],"representative_texts":["s｜标题｜电极材料用于储能。"],"mapping_status":"unmatched","matched_topics":[],"mapping_candidates":[{"node_id":"c","name":"储能材料","path":"能源 > 储能 > 储能材料","depth":2,"vector_score":.7,"score":.4}]}]})
  self.round.joinpath("cluster_tasks.jsonl").write_bytes(b'original tasks')
  self.round.joinpath("model_responses.jsonl").write_bytes(b'preserve model replies')
  self.manager=object.__new__(WorkflowManager);self.manager.root=self.package;self.manager.rounds_root=self.root/"workflow/rounds";self.manager.jobs={};self.manager.lock=threading.RLock()
 def write(self,name,data):self.round.joinpath(name).write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
 def test_reweight_saved_vectors_and_backup_without_model_calls(self):
  original=(self.clustering/"demo_topic_summary.json").read_bytes();reference=(self.round/"reference_taxonomy.json").read_bytes()
  with patch('subprocess.run',side_effect=AssertionError('must not start models')):
   result=self.manager.recalculate_reference_matches("R0001")
  self.assertEqual(result["before_counts"],{"unmatched":1})
  self.assertEqual(result["after_counts"],{"matched_single":1})
  self.assertEqual((self.round/result["backup"]/"demo_topic_summary.json").read_bytes(),original)
  self.assertEqual((self.round/result["backup"]/"cluster_tasks.jsonl").read_bytes(),b'original tasks')
  self.assertEqual((self.round/"model_responses.jsonl").read_bytes(),b'preserve model replies')
  self.assertEqual((self.round/"reference_taxonomy.json").read_bytes(),reference)
  self.assertEqual(self.manager.jobs,{})
 def test_active_reviewed_and_published_rounds_are_protected(self):
  for updates in [{"published":True},{"models":[{"model_id":"A","status":"completed"}]},{"status":"clustering"}]:
   self.write("round.json",{**self.state,**updates})
   with self.assertRaises(ValueError):self.manager.recalculate_reference_matches("R0001")
  self.write("round.json",self.state);self.manager.jobs={"j":{"round_id":"R0001","status":"running"}}
  with self.assertRaises(ValueError):self.manager.recalculate_reference_matches("R0001")
  self.assertFalse((self.round/"matching_history").exists())
