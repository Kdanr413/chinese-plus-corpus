import csv,json,tempfile,unittest
from pathlib import Path
from corpus_recognition.resource_curation import curate,decision,write_csv
from corpus_recognition.term import KnowledgeSeedTermExtractor
from corpus_topic_workflow.run_topic_demo import flatten_taxonomy

class ResourceCurationTests(unittest.TestCase):
 def test_generic_and_fragment_terms_do_not_pass_even_with_high_scores(self):
  for word in ['小时','就业','中国','...III','.动力电池','1.电动汽车','作为储能','指电池','熟悉新能源汽车','利用电池','巴西乙醇','级多晶硅','于纯电动汽车']:
   with self.subTest(word=word):self.assertNotEqual(decision(word,set())[0],'keep')
  for word in ['动力电池','光伏发电','锂离子电池','半导体','BMS','MPPT','高温地热资源']:
   with self.subTest(word=word):self.assertEqual(decision(word,set())[0],'keep')
 def test_full_audit_scores_upload_and_three_levels(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);terms=root/'terms.csv';tax=root/'taxonomy.json';ann=root/'annotations.jsonl';out=root/'out'
   write_csv(terms,[{'term_id':'t1','preferred_term':'动力电池','score':'0.8123'},{'term_id':'t2','preferred_term':'小时','score':'1.0'},{'term_id':'t3','preferred_term':'存疑词','score':'0.9'}],['term_id','preferred_term','score'])
   tax.write_text(json.dumps({'taxonomy_version':'3','nodes':[{'id':'a','name':'新能源','path':'新能源','keywords':[]},{'id':'b','name':'储能','path':'新能源 > 储能','parent_id':'a','keywords':[]},{'id':'c','name':'电池','path':'新能源 > 储能 > 电池','parent_id':'b','keywords':['动力电池','动力电池']}]}),encoding='utf-8')
   ann.write_text(json.dumps({'terms':['动力电池','小时'],'clean_text':'动力电池、动力电池。'})+'\n',encoding='utf-8')
   before=[p.read_bytes() for p in (terms,tax,ann)];m=curate(terms,tax,ann,out)
   self.assertEqual((m['kept_terms'],m['excluded_terms'],m['review_terms']),(1,1,1));self.assertEqual(m['keywords_missing'],0)
   row=next(csv.DictReader((out/'terminology.csv').read_text(encoding='utf-8-sig').splitlines()));self.assertEqual(row['reference_score'],'0.8123');self.assertEqual(row['score'],'0.8123');self.assertEqual(row['frequency'],'2')
   seed=KnowledgeSeedTermExtractor.from_csv(out/'terminology.csv');self.assertEqual(seed.seeds,{'动力电池':'t1'})
   nodes=flatten_taxonomy(json.loads((out/'reference_taxonomy.json').read_text(encoding='utf-8')));self.assertEqual(len(nodes),3);self.assertEqual(max(len(n['path'].split(' > ')) for n in nodes),3)
   self.assertEqual(len(json.loads((out/'classification_topics.json').read_text(encoding='utf-8'))['topics']),1)
   self.assertEqual(before,[p.read_bytes() for p in (terms,tax,ann)])
   with self.assertRaises(FileExistsError):curate(terms,tax,ann,out)
