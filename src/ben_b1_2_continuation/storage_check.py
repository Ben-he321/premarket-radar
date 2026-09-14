"""Repeat only fixed existing engineering cases after physical TEMP-table change."""
import sys
import shutil
from . import dense_check,crash_check
from .runtime import ROOT,write,read,sha,utc

def run(mode):
    target=ROOT/'engineering/storage_revision3'
    previous=ROOT/'engineering/dense_original/BLOCK_PROOFS.json'
    original=target/'engineering/dense_original/BLOCK_PROOFS.json'
    if not original.exists():
        original.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(previous,original)
    if sha(previous)!=sha(original):raise ValueError('EXISTING_ENGINEERING_BASELINE_CHANGED')
    if mode=='dense':
        if (target/'engineering/dense_bounded').exists():raise ValueError('STORAGE_DENSE_ALREADY_STARTED_NO_OVERWRITE')
        dense_check.ROOT=target;dense_check.run('bounded')
        proof=read(target/'engineering/DENSE_REAL_EQUIVALENCE.json')
        write(ROOT/'engineering/STORAGE_DENSE_REAL_EQUIVALENCE.json',{**proof,'at':utc(),'physical_temp_storage_revision':3,
            'original_baseline_sha256':sha(previous),'no_new_research_rule_or_parameter':True})
    elif mode=='crash':
        crash_check.ROOT=target;crash_check.run()
        proof=read(target/'engineering/REAL_DENSE_CRASH_RECOVERY.json')
        write(ROOT/'engineering/STORAGE_REAL_CRASH_RECOVERY.json',{**proof,'at':utc(),'physical_temp_storage_revision':3,
            'original_baseline_sha256':sha(previous),'no_new_research_rule_or_parameter':True})
    else:raise ValueError('UNKNOWN_FIXED_ENGINEERING_CASE')

if __name__=='__main__':run(sys.argv[1])
