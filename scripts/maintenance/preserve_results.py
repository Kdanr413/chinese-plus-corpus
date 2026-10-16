"""Copy key saved outputs and hash them; never run a processing stage."""
from pathlib import Path
import argparse,hashlib,json,shutil,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.workflow.file_paths import io_path

def preserve(root:Path,target:Path):
    root=root.resolve();target=target.resolve()
    if target.exists():raise ValueError('Backup target already exists')
    if not target.is_relative_to(root/'data/checkpoints'):raise ValueError('Backup must be within project data/checkpoints')
    candidates=[]
    for relative in ('data/curated','data/processed'):
        candidates.extend(p for p in (root/relative).rglob('*') if p.is_file())
    workflow=root/'data/workspaces/chinese-plus/topic-workflow'
    for sub in ('canonical','workflow/rounds'):
        for p in (workflow/sub).rglob('*'):
            if p.is_file() and (sub=='canonical' or 'models' in p.parts or p.name in {'round.json','reference_taxonomy.json','baseline_taxonomy.json','human_decisions.jsonl','published_changes.json','cluster_tasks.jsonl'}):candidates.append(p)
    jobs=workflow/'workflow/jobs.json'
    if jobs.is_file():candidates.append(jobs)
    io_path(target).mkdir(parents=True);manifest=[]
    for source in candidates:
        if not source.resolve().is_relative_to(root):raise ValueError('Source escapes project')
        destination=target/source.relative_to(root);io_path(destination.parent).mkdir(parents=True,exist_ok=True);before=io_path(source).stat();shutil.copy2(io_path(source),io_path(destination))
        with io_path(destination).open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        after=io_path(source).stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Result changed while backing up')
        manifest.append({'path':source.relative_to(root).as_posix(),'bytes':before.st_size,'sha256':digest})
    (target/'manifest.json').write_text(json.dumps({'files':manifest,'note':'All original files remain in place. Partial indexes and other workspace assets were not deleted or overwritten.'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'backed_up_files':len(manifest),'bytes':sum(x['bytes'] for x in manifest),'backup':str(target)},ensure_ascii=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();preserve(a.project,a.output)
