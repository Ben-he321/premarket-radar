"""Local same-checkpoint resume with disk temporary files on the active drive."""
import os
import sys
from pathlib import Path

repo=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repo))
root=Path('D:/BenAITradingData/ben-b1-2-continuation-20260914T154608Z')
if not root.is_dir():raise RuntimeError('EXISTING_VERIFIED_CONTINUATION_DIRECTORY_REQUIRED')
temporary=root/'_sqlite_tmp';temporary.mkdir(exist_ok=True)
os.environ['TEMP']=str(temporary);os.environ['TMP']=str(temporary)

if __name__=='__main__':
    from src.ben_b1_2_continuation.runner import main
    main()
