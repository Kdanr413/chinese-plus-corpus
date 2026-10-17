"""Re-score saved cluster candidates without embedding, clustering or model calls."""
import copy
import json
from collections import Counter
from .reference_terms import align_terms,configured_terms,lexical_components,combined_score
from .run_topic_demo import flatten_taxonomy,read_term_table,select_cluster_matches


def build_rematch(round_dir,project,seed,config):
    reference=json.loads((round_dir/"reference_taxonomy.json").read_text(encoding="utf-8-sig"))
    nodes=flatten_taxonomy(reference);by_id={n["id"]:n for n in nodes}
    terms,_=configured_terms(project,seed,read_term_table)
    terms,links=align_terms(terms,nodes)
    summary=json.loads((round_dir/"clustering/demo_topic_summary.json").read_text(encoding="utf-8-sig"))
    changed=copy.deepcopy(summary);samples={}
    path=round_dir/"clustering/demo_sampled_units.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8-sig") as source:
            for line in source:
                if line.strip():
                    row=json.loads(line);samples[row["unit_id"]]=row["analysis_text"]
    weights=config["matching"]["weights"];threshold=config["matching"]["cluster_match_threshold"]
    before=Counter();after=Counter();units=Counter();old_units=0;total=0
    for row in changed["topics"]:
        if row["cluster_id"]=="BT_OUTLIER":continue
        before[row["mapping_status"]]+=1
        size=int(row.get("unit_count",0));total+=size
        if row.get("matched_topics"):old_units+=size
        words=[word.get("term","") if isinstance(word,dict) else str(word) for word in row.get("representative_words",[])]
        texts=[]
        for text in row.get("representative_texts",[]):
            parts=str(text).split("｜",2)
            texts.append(samples.get(parts[0],parts[-1])[:800])
        text=" ".join(words+texts);candidates=[]
        for old in row.get("mapping_candidates",[]):
            node=by_id.get(old.get("node_id"))
            if not node or node.get("assignable") is False or node.get("depth")!=2:continue
            evidence=lexical_components(node,text,terms)
            evidence["vector_score"]=float(old.get("vector_score",0))
            score,effective=combined_score(evidence,weights)
            candidate={**old,**evidence,"name":node["name"],"path":node["path"],"score":score,"effective_weights":effective}
            candidates.append(candidate)
        candidates.sort(key=lambda c:(c["score"],c["depth"],c["node_id"]),reverse=True)
        matches=select_cluster_matches(candidates,threshold)
        row["mapping_candidates"]=candidates;row["matched_topics"]=matches
        row["mapping_status"]="matched_multiple" if len(matches)>1 else "matched_single" if matches else "unmatched"
        after[row["mapping_status"]]+=1;units[row["mapping_status"]]+=size
    count=sum(after.values());matched=count-after["unmatched"];matched_units=total-units["unmatched"]
    report={"method":"saved_semantic_scores_with_current_reference_lexical_and_term_evidence",
        "weights":weights,"threshold":threshold,"term_alignment":links,
        "before_counts":dict(before),"after_counts":dict(after),"nonoutlier_clusters":count,
        "matched_cluster_rate":matched/count if count else 0,"nonoutlier_units":total,
        "before_matched_units":old_units,"matched_units":matched_units,
        "matched_unit_rate":matched_units/total if total else 0,
        "outlier_units":sum(int(r.get("unit_count",0)) for r in summary["topics"] if r["cluster_id"]=="BT_OUTLIER"),
        "note":"复用原聚类和已保存语义分数；不重编码术语原型、不修改逐句诊断与模型回复；重算不会自动发布。"}
    return changed,report
