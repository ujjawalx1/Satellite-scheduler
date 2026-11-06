from datetime import datetime,timedelta
from types import SimpleNamespace
from app.core import pick_job,score

def job(priority,age,cpu=2,mem=1024):
    return SimpleNamespace(priority=priority,created_at=datetime.utcnow()-timedelta(seconds=age),required_cpu=cpu,required_memory_mb=mem)
def test_emergency_first():
    assert pick_job([job('LOW',1),job('EMERGENCY',1)],4,4096).priority=='EMERGENCY'
def test_aging_prevents_starvation():
    assert pick_job([job('LOW',180),job('EMERGENCY',1)],4,4096).priority=='LOW'
def test_resource_fit():
    assert pick_job([job('EMERGENCY',1,8),job('NORMAL',1,2)],4,4096).priority=='NORMAL'
def test_no_eligible():
    assert pick_job([job('HIGH',1,8)],2,1024) is None
