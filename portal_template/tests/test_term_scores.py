import csv
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock,patch
from corpus_recognition.term_scores import term_metadata,terminology_with_scores


def write_csv(path,fields,rows):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open("w",encoding="utf-8-sig",newline="") as handle:
  writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)


class TermScoreTests(unittest.TestCase):
 def fixture(self,root):
  path=root/"work/recognition/08_approved_resources/terminology.csv"
  write_csv(path,["term_id","preferred_term","score"],[{"term_id":"a","preferred_term":"动力电池","score":""},{"term_id":"b","preferred_term":"零分术语","score":0},{"term_id":"c","preferred_term":"人工新增","score":""}])
  table=path.parents[1]/"05_term_validation/term_validation_table.csv"
  write_csv(table,["normalized_form","score"],[{"normalized_form":"动力电池","score":0.4},{"normalized_form":"动力电池","score":0.8},{"normalized_form":"人工新增","score":"NaN"},{"normalized_form":"未通过术语","score":1}])
  return path,table

 def test_historical_download_scores_are_filled_without_changing_original(self):
  with tempfile.TemporaryDirectory() as temp:
   path,table=self.fixture(Path(temp));original=path.read_bytes();source=table.read_bytes()
   export=terminology_with_scores(path)
   with export.open(encoding="utf-8-sig",newline="") as handle:rows=list(csv.DictReader(handle))
   self.assertEqual(len(rows),3);self.assertEqual(rows[0]["score"],"0.8")
   self.assertEqual(rows[0]["score_source"],"saved_term_validation_max")
   self.assertEqual(rows[1]["score"],"0");self.assertEqual(rows[1]["score_status"],"available")
   self.assertEqual(rows[2]["score"],"");self.assertEqual(rows[2]["score_status"],"not_recorded")
   self.assertEqual(path.read_bytes(),original);self.assertEqual(table.read_bytes(),source)
   stamp=export.stat().st_mtime_ns;terminology_with_scores(path);self.assertEqual(export.stat().st_mtime_ns,stamp)
   write_csv(table,["normalized_form","score"],[{"normalized_form":"动力电池","score":0.9}])
   terminology_with_scores(path)
   with export.open(encoding="utf-8-sig",newline="") as handle:self.assertEqual(next(csv.DictReader(handle))["score"],"0.9")

 def test_best_score_retains_identity_and_zero(self):
  rows=[{"normalized_form":"术语","candidate_term_id":"first","score":None},{"normalized_form":"术语","candidate_term_id":"second","score":0},{"normalized_form":"术语","score":0.7}]
  result=term_metadata(rows)["术语"]
  self.assertEqual(result["score"],0.7);self.assertEqual(result["candidate_term_id"],"first")
  self.assertIsNone(rows[0]["score"])

 def test_both_download_routes_use_scored_export(self):
  from corpus_api import recognition as api
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);path,_=self.fixture(root);rid=uuid.uuid4()
   repo=SimpleNamespace(get_run=lambda _: {"workspace_path":"work"},get_run_bundle=lambda _: {})
   coordinator=SimpleNamespace(registry=SimpleNamespace(data_root=root))
   with patch.object(api,"require_admin"),patch.object(api,"REPOSITORY_FACTORY",return_value=repo),patch.object(api,"COORDINATOR_FACTORY",return_value=coordinator),patch.object(api,"_pending_human_review_scopes",return_value=[]):
    for response in [api.download_artifact(rid,"final-terminology",None),api.download_recognition_asset(rid,"approved-terms",None)]:
     self.assertEqual(Path(response.path).name,"terminology_with_scores.csv")

 def test_approved_bundle_selects_score(self):
  from corpus_recognition.repository import RecognitionRepository
  repo=RecognitionRepository.__new__(RecognitionRepository);repo.settings=None
  connection=MagicMock();connection.execute.return_value.fetchall.return_value=[]
  with patch.object(repo,"get_run",return_value={}),patch("corpus_recognition.repository.connect") as connect:
   connect.return_value.__enter__.return_value=connection
   repo.get_approved_resource_bundle(uuid.uuid4())
  queries=[call.args[0] for call in connection.execute.call_args_list if "FROM term_candidates" in call.args[0]]
  self.assertIn("score",queries[0].split("FROM")[0])
