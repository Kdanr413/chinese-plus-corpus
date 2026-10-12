"""Private per-project step-model settings and tested-configuration fingerprints."""
from __future__ import annotations
import hashlib,json,threading
from pathlib import Path
LOCK=threading.RLock()
STAGES=('cleaning','term','cross')
MODEL_KEYS=('id','model','model_version','base_url','api_key','weight')
def settings_path(root):return Path(root)/'configs/llm/step_models.json'
def defaults():return {'models':[],'steps':{s:{'mode':'off' if s=='cross' else 'rules','model_ids':[],'strategy':'majority','min_validators':1} for s in STAGES},'_tests':{}}
def read_settings(root):
    with LOCK:
        p=settings_path(root)
        return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else defaults()
def model_signature(rows):
    return [{k:(row.get('model_version') or row.get('model','')) if k=='model_version' else row.get(k,1.0 if k=='weight' else '') for k in MODEL_KEYS} for row in rows]
def selected_models(config,stage):
    step=config['steps'][stage]
    if step['mode'] not in {'models','hybrid'}:return []
    bank={row['id']:row for row in config['models']}
    return [bank[key] for key in step['model_ids']]
def validate_settings(config):
    if set(config['steps'])!=set(STAGES):raise ValueError('必须配置清洗、术语和交叉检验三个步骤')
    ids=[x['id'] for x in config['models']]
    if len(ids)!=len(set(ids)):raise ValueError('模型配置名称不能重复')
    for stage in STAGES:
        step=config['steps'][stage]
        if len(step['model_ids'])!=len(set(step['model_ids'])) or any(x not in ids for x in step['model_ids']):raise ValueError('步骤选择了不存在或重复的模型')
        rows=selected_models(config,stage)
        if step['mode'] in {'models','hybrid'}:
            if not rows:raise ValueError('启用外部模型的步骤至少选择一个模型')
            if any(not all(row.get(k,'').strip() for k in ['id','model','base_url','api_key']) for row in rows):raise ValueError('请完整配置已选择的模型')
            if stage=='cleaning' and len(rows)!=1:raise ValueError('清洗步骤必须明确选择一个模型')
            if step['min_validators']>len(rows):raise ValueError('最少有效模型超过本步骤选择的模型数量')
        if stage=='cleaning' and step['mode'] not in {'rules','models'}:raise ValueError('清洗请选择规则或外部模型')
        if stage=='term' and step['mode'] not in {'rules','models','hybrid'}:raise ValueError('术语识别方式无效')
def fingerprint(config,stage,prompt):
    value={'step':config['steps'][stage],'models':model_signature(selected_models(config,stage)),'prompt':{k:prompt.get(k,'') for k in ['system_prompt','task_instruction']}}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
def write_settings(root,config):
    p=settings_path(root);p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix('.json.tmp');tmp.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');tmp.replace(p)
def save_settings(root,config):
    validate_settings(config)
    with LOCK:
        config={**config,'_tests':read_settings(root).get('_tests',{})}
        write_settings(root,config)
        return config
def verified(root,stage,prompt):
    config=read_settings(root)
    return config.get('_tests',{}).get(stage)==fingerprint(config,stage,prompt)
def mark_verified(root,stage,models,prompt,expected_fingerprint=None):
    with LOCK:
        if not settings_path(root).exists():return False
        config=read_settings(root)
        if expected_fingerprint is not None and fingerprint(config,stage,prompt)!=expected_fingerprint:return False
        if not selected_models(config,stage) or model_signature(models)!=model_signature(selected_models(config,stage)):return False
        config.setdefault('_tests',{})[stage]=fingerprint(config,stage,prompt);write_settings(root,config);return True
def require_verified(root,stage,prompt):
    if not verified(root,stage,prompt):raise ValueError('请先保存并测试当前'+{'cleaning':'清洗','term':'术语','cross':'交叉检验'}[stage]+'模型配置与正式提示词')
