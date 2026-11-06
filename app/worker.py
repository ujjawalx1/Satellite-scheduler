import os,time,json,uuid,signal,sys
from datetime import timedelta
from sqlalchemy import select, or_
from .core import SessionLocal,Job,Worker,now,event,pick_job,MAX_ATTEMPTS,log,Base,engine

WORKER_ID=os.getenv('WORKER_ID',str(uuid.uuid4())[:8]); CPU=int(os.getenv('WORKER_CPU','4')); MEMORY=int(os.getenv('WORKER_MEMORY_MB','8192'))
POLL=float(os.getenv('POLL_SECONDS','0.5')); TIME_SCALE=float(os.getenv('TIME_SCALE','0.01'))
LEASE_SECONDS=int(os.getenv('LEASE_SECONDS','15'))

def recover_stale(s):
    cutoff=now()-timedelta(seconds=LEASE_SECONDS)
    stale=s.query(Job).filter(Job.status=='RUNNING', or_(Job.last_heartbeat.is_(None), Job.last_heartbeat<cutoff)).all()
    for j in stale:
        j.status='QUEUED' if j.attempts<MAX_ATTEMPTS else 'FAILED'
        j.worker_id=None; j.last_heartbeat=None
        event(s,'JOB_RECOVERED' if j.status=='QUEUED' else 'JOB_FAILED',j.id,{'reason':'worker heartbeat expired'})
    s.commit()

def claim(s):
    # PostgreSQL row-level locks prevent multiple workers claiming the same job.
    dialect = s.bind.dialect.name if s.bind else ''
    if dialect == 'postgresql':
        q=s.query(Job).filter_by(status='QUEUED').order_by(Job.created_at).with_for_update(skip_locked=True)
    else:
        q=s.query(Job).filter_by(status='QUEUED').order_by(Job.created_at)
    candidates=q.limit(500).all()
    j=pick_job(candidates,CPU,MEMORY)
    if j:
        j.status='RUNNING';j.started_at=now();j.last_heartbeat=now();j.worker_id=WORKER_ID;j.attempts+=1
        event(s,'JOB_STARTED',j.id,{'worker':WORKER_ID,'attempt':j.attempts});s.commit()
    return j

def process(job_id):
    with SessionLocal() as s:
        j=s.get(Job,job_id)
        if not j: return
        # Synthetic processing: scaled by image size and requested CPU, bounded for demo.
        seconds=min(12,max(0.2,j.size_mb/max(j.required_cpu,1)*TIME_SCALE))
        steps=max(1,int(seconds/0.5))
    for _ in range(steps):
        time.sleep(seconds/steps)
        with SessionLocal() as s:
            j=s.get(Job,job_id)
            if not j or j.status!='RUNNING' or j.worker_id!=WORKER_ID:return
            j.last_heartbeat=now();s.commit()
    with SessionLocal() as s:
        j=s.get(Job,job_id)
        if not j or j.status!='RUNNING' or j.worker_id!=WORKER_ID:return
        result={'processing_type':j.processing_type,'image_name':j.image_name,'status':'simulated_success',
                'size_mb':j.size_mb,'coordinates':[j.latitude,j.longitude],
                'note':'Synthetic result; no actual raster imagery processed'}
        j.result=json.dumps(result);j.status='COMPLETED';j.finished_at=now()
        event(s,'JOB_COMPLETED',j.id,result);s.commit()

def main():
    log.info('Starting worker %s CPU=%s RAM=%s',WORKER_ID,CPU,MEMORY)
    for attempt in range(15):
        try:
            Base.metadata.create_all(engine)
            with SessionLocal() as s:
                s.merge(Worker(id=WORKER_ID,cpu=CPU,memory_mb=MEMORY,last_seen=now()));s.commit()
            break
        except Exception as e:
            if attempt == 14:
                log.error("Worker failed to connect to database: %s", e)
                raise
            log.warning("Worker waiting for database (attempt %d/15): %s; retrying in 2s...", attempt + 1, e)
            time.sleep(2)
    while True:
        try:
            with SessionLocal() as s:
                w=s.get(Worker,WORKER_ID)
                if w:
                    w.last_seen=now();w.active_job=None;s.commit()
                recover_stale(s)
                j=claim(s)
                if j:
                    if w:
                        w.active_job=j.id;s.commit()
                    job_id=j.id
                else:job_id=None
            if job_id:process(job_id)
            else:time.sleep(POLL)
        except Exception:
            log.exception('Worker error; will retry')
            time.sleep(2)

if __name__=='__main__':main()
