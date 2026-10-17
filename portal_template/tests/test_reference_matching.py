import unittest
from corpus_topic_workflow.reference_matching import match_cluster_reference


def node(tid,name,keywords):
    return {"id":tid,"name":name,"path":"技术 > 应用 > "+name,"assignable":True,"keywords":keywords}


class ReferenceMatchingTests(unittest.TestCase):
    def setUp(self):
        self.battery=node("battery","动力电池",["锂离子电池","荷电状态"])
        self.solar=node("solar","光伏发电",["太阳能电池","逆变器"])
        self.nodes=[self.battery,self.solar]

    def test_saved_matches_reuse_frozen_names_ids_and_evidence(self):
        task={"machine_matches":[{"node_id":"battery","name":"不能采用的名称","score":.8,"keyword_hits":["荷电状态"]}]}
        row=match_cluster_reference(task,self.nodes)[0]
        self.assertEqual((row["topic_id"],row["name"],row["path"]),("battery","动力电池",self.battery["path"]))
        self.assertEqual(row["method"],"saved_machine_match")

    def test_no_vector_only_foreign_low_or_malformed_scores(self):
        for match in [{"node_id":"battery","score":.9}, {"node_id":"foreign","score":.9,"keyword_hits":["电池"]}, {"node_id":"battery","score":.1,"keyword_hits":["电池"]}, {"node_id":"battery","score":"NaN","keyword_hits":["电池"]}, {"node_id":"battery","score":None,"keyword_hits":["电池"]}]:
            with self.subTest(match=match):self.assertEqual(match_cluster_reference({"machine_matches":[match]},self.nodes),[])

    def test_keywords_can_name_legacy_clusters_without_external_models(self):
        task={"representative_words":[{"term":"荷电状态"}],"representative_texts":["锂离子电池需要测量荷电状态"]}
        self.assertEqual(match_cluster_reference(task,self.nodes)[0]["name"],"动力电池")

    def test_name_evidence_and_ties(self):
        self.assertEqual(match_cluster_reference({"representative_texts":["动力电池的运行"]},self.nodes)[0]["name"],"动力电池")
        self.assertEqual(match_cluster_reference({"representative_texts":["动力电池和光伏发电"]},self.nodes),[])

    def test_generic_single_keyword_and_ascii_substring_do_not_match(self):
        self.assertEqual(match_cluster_reference({"representative_texts":["逆变器运行正常"]},self.nodes),[])
        self.assertEqual(match_cluster_reference({"representative_texts":["HVAC系统"]},[node("ac","AC",[])]),[])

    def test_out_of_scope_inactive_and_nonleaf_do_not_match(self):
        for override in [{"scope_status":"OUT_OF_SCOPE"},{"status":"inactive"},{"assignable":False},{"path":"技术 > 动力电池"}]:
            self.assertEqual(match_cluster_reference({"representative_texts":["动力电池"]},[{**self.battery,**override}]),[])
