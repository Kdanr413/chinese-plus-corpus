"""Use frozen reference names for clusters; no external model or invented names."""
import re,math


def match_cluster_reference(task,nodes,threshold=0.46):
    leaves=[n for n in nodes if n.get("assignable") is not False and len(str(n.get("path","")).split(" > "))==3 and n.get("status","active")=="active" and n.get("scope_status","IN_SCOPE")!="OUT_OF_SCOPE"]
    by_path={n["path"]:n for n in leaves};by_id={str(n["id"]):n for n in leaves}
    selected=[]
    for match in task.get("machine_matches",[]):
        node=by_path.get(match.get("path")) or by_id.get(str(match.get("node_id","")))
        try:score=float(match.get("score",0))
        except (TypeError,ValueError):continue
        if node is None or not math.isfinite(score) or score<threshold or not (match.get("keyword_hits") or match.get("term_hits")):
            continue
        if any(row["path"]==node["path"] for row in selected):continue
        selected.append({"path":node["path"],"topic_id":node["id"],"name":node["name"],"method":"saved_machine_match","score":match["score"],"keyword_hits":match.get("keyword_hits",[]),"term_hits":match.get("term_hits",[])})
    if selected:return selected
    # Older rounds may lack saved vector matches. Require discriminative lexical
    # evidence, and abstain on ties rather than guessing from representative words.
    words={str(w.get("term","") if isinstance(w,dict) else w).strip() for w in task.get("representative_words",[])}
    text=" ".join([*words,*[str(t)[:800] for t in task.get("representative_texts",[])[:8]]]).casefold()
    usage={}
    for node in leaves:
        for word in set(node.get("keywords",[])):usage[word.casefold()]=usage.get(word.casefold(),0)+1
    def hit(value):
        value=str(value).strip().casefold()
        if len(value)<2:return False
        if value.isascii():return re.search(r"(?<![a-z0-9])"+re.escape(value)+r"(?![a-z0-9])",text) is not None
        return value in text
    ranked=[]
    for node in leaves:
        name_hit=hit(node["name"])
        hits=sorted({w for w in node.get("keywords",[]) if hit(w)})
        unique_exact=any(w in words and len(w)>=4 and usage.get(w.casefold())==1 for w in hits)
        if not (name_hit or len(hits)>=2 or unique_exact):continue
        weight=2*int(name_hit)+len(hits)
        ranked.append((weight,{"path":node["path"],"topic_id":node["id"],"name":node["name"],"method":"reference_name_and_keywords","score":weight,"keyword_hits":hits,"name_hit":name_hit}))
    ranked.sort(key=lambda item:(-item[0],item[1]["path"]))
    if not ranked or (len(ranked)>1 and ranked[0][0]==ranked[1][0]):return []
    return [ranked[0][1]]
