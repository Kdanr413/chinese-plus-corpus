"""Align approved terms to the frozen reference and share scoring components."""
import copy
import json
import re
import unicodedata
from collections import defaultdict


class AlignedTerms(list):
    """Build once; scoring must not scan the entire terminology for every node."""
    def __init__(self,rows):
        super().__init__(rows)
        self.by_topic=defaultdict(list)
        for row in rows:
            if row["approved"]:
                for node_id in row["topic_node_ids"]:self.by_topic[node_id].append(row)


def normalized(value):
    return unicodedata.normalize("NFKC",str(value)).strip().casefold()


def align_terms(rows,nodes):
    leaves=[n for n in nodes if n.get("assignable") is not False and n.get("depth")==2 and n.get("status","active")=="active"]
    ids={n["id"] for n in leaves};names=defaultdict(set);keywords=defaultdict(set)
    for node in leaves:
        names[normalized(node["name"])].add(node["id"])
        for word in node.get("keywords",[]):keywords[normalized(word)].add(node["id"])
    linked=[];audit=[];seen=set()
    for original in rows:
        row=copy.deepcopy(original)
        key=normalized(row["term"])
        if key in seen:continue
        seen.add(key)
        old=set(row.get("topic_node_ids",[]));valid=old&ids
        forms={normalized(word) for word in row["forms"] if len(normalized(word))>=2}
        if valid:method="existing_reference_id"
        else:
            valid=set().union(*(names.get(word,set()) for word in forms));method="exact_reference_name"
            if not valid:
                valid=set().union(*(keywords.get(word,set()) for word in forms));method="exact_reference_keyword" if valid else "unresolved"
            if valid and (len(valid)>1 or method=="exact_reference_keyword"):
                # Candidate links are weaker than an explicitly curated ID link.
                row["weight"]=min(float(row.get("weight",1)),0.5);row["role"]="related"
        row["topic_node_ids"]=sorted(valid);row["link_method"]=method
        row["association_needs_review"]=method!="existing_reference_id" and (len(valid)>1 or method in {"exact_reference_keyword","unresolved"})
        linked.append(row)
        audit.append({"term":row["term"],"old_ids":sorted(old),"current_ids":sorted(valid),"method":method,"needs_review":row["association_needs_review"]})
    return AlignedTerms(linked),{"terms":len(linked),"linked_terms":sum(bool(x["topic_node_ids"]) for x in linked),"unresolved_terms":sum(not x["topic_node_ids"] for x in linked),"links":audit}


def configured_terms(project,seed,reader):
    rows,source=reader(seed)
    config=project/"configs/pipeline.json"
    if config.is_file():
        linguistic=json.loads(config.read_text(encoding="utf-8-sig")).get("linguistic",{})
        for key in ("terminology","discovered_terminology"):
            value=linguistic.get(key)
            if not value:continue
            path=(project/value).resolve()
            if not path.is_relative_to(project.resolve()):continue
            extra,_=reader(path)
            rows=extra+rows
    return rows,source


def lexical_components(node,text,rows):
    lowered=text.casefold()
    keywords=sorted({word for word in [node["name"],*node.get("keywords",[])] if word and word.casefold() in lowered})
    name_hit=node["name"] in keywords
    keyword_score=min(1.0,(2*name_hit+len(keywords)-name_hit)/4)
    terms=[];positive=negative=0.0
    index=getattr(rows,"by_topic",None)
    relevant=index.get(node["id"],[]) if index is not None else [row for row in rows if row["approved"] and node["id"] in row["topic_node_ids"]]
    for row in relevant:
        hits=[form for form in row["forms"] if form.casefold() in lowered]
        if not hits:continue
        terms.extend(hits)
        if row["role"]=="negative":negative+=row["weight"]
        else:positive+=row["weight"]*(1.25 if row["role"]=="core" else 1)
    return {"keyword_hits":keywords,"term_hits":sorted(set(terms)),"keyword_score":keyword_score,
            "term_score":max(0,min(1,positive/2.5)-min(.5,negative/2)),
            "hierarchy_score":min(1,.35+.25*int(node["depth"])) if keywords or terms else 0,
            "has_linked_terms":bool(relevant)}


def combined_score(parts,weights):
    active=dict(weights)
    if not parts["has_linked_terms"]:active["approved_terms"]=0
    total=sum(active.values())
    effective={key:value/total for key,value in active.items()}
    score=sum(effective[key]*parts[field] for key,field in [("vector","vector_score"),("approved_terms","term_score"),("taxonomy_keywords","keyword_score"),("hierarchy","hierarchy_score")])
    return round(score,6),effective
