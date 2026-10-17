import unittest
from corpus_topic_workflow.reference_terms import align_terms,lexical_components,combined_score


def term(name,ids):
 return {"term":name,"forms":[name],"topic_node_ids":ids,"approved":True,"role":"core","weight":1}


class ReferenceTermTests(unittest.TestCase):
 def setUp(self):
  self.nodes=[{"id":"v3","name":"动力电池","path":"能源 > 交通 > 动力电池","depth":2,"assignable":True,"keywords":["锂离子电池"]}]
 def test_stale_id_relinks_by_name_and_unresolved_id_is_removed(self):
  original=[term("动力电池",["OLD"]),term("不存在的词",["BAD"])]
  linked,audit=align_terms(original,self.nodes)
  self.assertEqual(linked[0]["topic_node_ids"],["v3"])
  self.assertEqual(linked[1]["topic_node_ids"],[])
  self.assertEqual(original[0]["topic_node_ids"],["OLD"])
  self.assertEqual(audit["unresolved_terms"],1)
 def test_valid_id_is_preserved_and_keyword_link_is_evidenced(self):
  linked,audit=align_terms([term("已有术语",["v3"]),term("锂离子电池",["OLD"])],self.nodes)
  self.assertEqual(audit["links"][0]["method"],"existing_reference_id")
  self.assertEqual(audit["links"][1]["method"],"exact_reference_keyword")
  self.assertEqual(linked[1]["topic_node_ids"],["v3"])
 def test_ambiguous_links_are_candidates_with_lower_weight(self):
  nodes=self.nodes+[{**self.nodes[0],"id":"other","name":"其他电池"}]
  linked,_=align_terms([term("锂离子电池",["OLD"])],nodes)
  self.assertTrue(linked[0]["association_needs_review"])
  self.assertEqual(linked[0]["weight"],.5)
 def test_unavailable_term_weight_is_redistributed_not_fabricated(self):
  parts=lexical_components(self.nodes[0],"锂离子电池",[]);parts["vector_score"]=.7
  score,effective=combined_score(parts,{"vector":.35,"approved_terms":.15,"taxonomy_keywords":.45,"hierarchy":.05})
  self.assertEqual(parts["term_score"],0)
  self.assertEqual(effective["approved_terms"],0)
  self.assertAlmostEqual(sum(effective.values()),1)
  self.assertGreater(score,.46)
 def test_term_hit_uses_current_reference_ids(self):
  rows,_=align_terms([term("动力电池",["OLD"])],self.nodes)
  parts=lexical_components(self.nodes[0],"动力电池的应用",rows)
  self.assertGreater(parts["term_score"],0)
  self.assertEqual(parts["term_hits"],["动力电池"])

 def test_full_candidate_scorer_uses_the_shared_weights(self):
  import numpy as np
  from corpus_topic_workflow.run_topic_demo import score_node_candidates
  nodes=[{**self.nodes[0],"node_type":"main_tree"}]
  result=score_node_candidates(text="锂离子电池",vector=np.array([1.,0.]),nodes=nodes,prototype_vectors=np.array([[.7,.71414284]]),term_rows=[],np=np,allowed_types={"main_tree"})
  self.assertEqual(result[0]["effective_weights"]["approved_terms"],0)
  self.assertGreater(result[0]["score"],.46)

 def test_approved_terms_are_indexed_by_current_topic_once(self):
  rows,_=align_terms([term("动力电池",["OLD"]),term("未关联词",["BAD"])],self.nodes)
  self.assertEqual([row["term"] for row in rows.by_topic["v3"]],["动力电池"])
  self.assertNotIn("OLD",rows.by_topic)
