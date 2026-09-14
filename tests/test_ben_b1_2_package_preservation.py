"""Temporary mock files only: whole recovery-copy preservation checks."""
import pytest
from src.ben_b1_2_continuation.package import recheck_sealed_storage_source
from src.ben_b1_2_continuation.runtime import write,read,sha


def sealed(tmp_path):
    source=tmp_path/'sealed';source.mkdir();rows=[]
    for name,contents in [('checkpoint.json','mock checkpoint'),('replay.sqlite','mock database'),
                          ('replay.sqlite-wal','mock uncheckpointed bytes'),('replay.sqlite-shm','mock shared memory')]:
        p=source/name;p.write_text(contents,encoding='utf-8');s=p.stat()
        rows.append({'relative_path':name,'after':{'bytes':s.st_size,'mtime_ns':s.st_mtime_ns,'sha256':sha(p)}})
    proof=tmp_path/'proof.json';write(proof,{'status':'PASS','source':str(source),'files':rows})
    return source,proof,tmp_path/'result.json'


def test_full_mock_recovery_fileset_is_read_only(tmp_path):
    source,proof,out=sealed(tmp_path)
    result=recheck_sealed_storage_source(proof,out)
    assert result['status']=='PASS' and result['checked_files']==4
    assert not result['source_database_opened']
    assert {p.name for p in source.iterdir()}=={'checkpoint.json','replay.sqlite','replay.sqlite-wal','replay.sqlite-shm'}


@pytest.mark.parametrize('fault',['wal_changed','shm_missing','extra_file'])
def test_preservation_failure_retains_explicit_evidence(tmp_path,fault):
    source,proof,out=sealed(tmp_path)
    if fault=='wal_changed':(source/'replay.sqlite-wal').write_text('changed mock WAL',encoding='utf-8')
    elif fault=='shm_missing':(source/'replay.sqlite-shm').unlink()
    else:(source/'unexpected.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='SEALED_C_STORAGE_SOURCE_CHANGED'):
        recheck_sealed_storage_source(proof,out)
    result=read(out)
    assert result['status']=='FAIL'
    if fault=='wal_changed':assert any(not r['unchanged'] for r in result['files'])
    else:assert not result['file_set_unchanged']
