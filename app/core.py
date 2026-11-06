import os, json, uuid, time, logging, threading
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from typing import Optional
import jwt
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Integer, Float, DateTime, Text, ForeignKey, func, select, update, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('satellite')

if os.getenv('POSTGRES_HOST'):
    DB_URL = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER", "satellite"),
        password=os.getenv("POSTGRES_PASSWORD", "satellite-local-only"),
        host=os.getenv("POSTGRES_HOST", "db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "satellite"),
    )
elif os.getenv('DATABASE_URL'):
    DB_URL = os.getenv('DATABASE_URL')
else:
    DB_URL = 'sqlite:///./satellite.db'

SECRET = os.getenv('JWT_SECRET', 'change-this-secret-for-production')
engine = create_engine(DB_URL, connect_args={'check_same_thread':False} if str(DB_URL).startswith('sqlite') else {}, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
def now(): return datetime.now(timezone.utc).replace(tzinfo=None)

class User(Base):
    __tablename__='users'
    id=Column(String,primary_key=True); username=Column(String,unique=True,nullable=False); role=Column(String,default='user')
class Job(Base):
    __tablename__='jobs'
    id=Column(String,primary_key=True); owner=Column(String,ForeignKey('users.id'),nullable=False)
    idempotency_key=Column(String,nullable=True)
    image_name=Column(String,nullable=False); size_mb=Column(Integer,nullable=False)
    processing_type=Column(String,nullable=False); priority=Column(String,nullable=False)
    latitude=Column(Float,nullable=False); longitude=Column(Float,nullable=False)
    required_cpu=Column(Integer,nullable=False); required_memory_mb=Column(Integer,nullable=False)
    status=Column(String,default='QUEUED',index=True); created_at=Column(DateTime,default=now)
    started_at=Column(DateTime); finished_at=Column(DateTime); worker_id=Column(String)
    attempts=Column(Integer,default=0); error=Column(Text); result=Column(Text)
    last_heartbeat=Column(DateTime)
class Event(Base):
    __tablename__='events'
    id=Column(String,primary_key=True); job_id=Column(String,index=True); kind=Column(String)
    payload=Column(Text); created_at=Column(DateTime,default=now)
class Worker(Base):
    __tablename__='workers'
    id=Column(String,primary_key=True); cpu=Column(Integer); memory_mb=Column(Integer)
    active_job=Column(String); last_seen=Column(DateTime,default=now)

PRIORITY={'LOW':0,'NORMAL':10,'HIGH':20,'EMERGENCY':40}
MAX_ATTEMPTS=3

def db():
    with SessionLocal() as s: yield s

def event(s, kind, job_id=None, payload=None):
    s.add(Event(id=str(uuid.uuid4()),kind=kind,job_id=job_id,payload=json.dumps(payload or {})))
    log.info('%s job=%s payload=%s',kind,job_id,payload)

def score(job, timestamp=None):
    created = getattr(job, 'created_at', None) or now()
    elapsed = max(0.0, ((timestamp or now()) - created).total_seconds())
    return PRIORITY.get(getattr(job, 'priority', 'NORMAL'), 10) + elapsed / 3.0  # aging: every 30s adds 10 points

def pick_job(jobs, cpu, memory, timestamp=None):
    eligible=[j for j in jobs if j.required_cpu<=cpu and j.required_memory_mb<=memory]
    return max(eligible,key=lambda j:(score(j,timestamp),-j.created_at.timestamp())) if eligible else None

class JobInput(BaseModel):
    image_name:str=Field(min_length=3,max_length=200)
    size_mb:int=Field(ge=1,le=100000)
    processing_type:str=Field(pattern='^(flood_mapping|vegetation_index|cloud_detection|thumbnail|land_classification)$')
    priority:str=Field(default='NORMAL',pattern='^(LOW|NORMAL|HIGH|EMERGENCY)$')
    latitude:float=Field(ge=-90,le=90); longitude:float=Field(ge=-180,le=180)
    required_cpu:int=Field(ge=1,le=16); required_memory_mb:int=Field(ge=128,le=65536)

security=HTTPBearer()
def auth(creds:HTTPAuthorizationCredentials=Depends(security),s:Session=Depends(db)):
    try: payload=jwt.decode(creds.credentials,SECRET,algorithms=['HS256'])
    except jwt.PyJWTError: raise HTTPException(401,'Invalid or expired token')
    u=s.get(User,payload.get('sub'))
    if not u: raise HTTPException(401,'Unknown user')
    return u

def admin(u:User=Depends(auth)):
    if u.role!='admin': raise HTTPException(403,'Admin only')
    return u

class LoginInput(BaseModel):
    username:str=Field(min_length=1)
    password:str=Field(min_length=1)

@asynccontextmanager
async def lifespan(app):
    for attempt in range(15):
        try:
            Base.metadata.create_all(engine)
            with SessionLocal() as s:
                for name,role in [('student','user'),('admin','admin')]:
                    if not s.query(User).filter_by(username=name).first():
                        s.add(User(id=name,username=name,role=role))
                s.commit()
            log.info("Database initialized successfully.")
            break
        except Exception as e:
            if attempt == 14:
                log.error("Could not initialize database: %s", e)
                raise
            log.warning("Database not ready yet (attempt %d/15): %s; retrying in 2s...", attempt + 1, e)
            time.sleep(2)
    yield

app=FastAPI(title='Satellite Image Processing Job Scheduler',version='1.0.0',lifespan=lifespan)

@app.get('/health')
def health(): return {'status':'ok','service':'satellite-scheduler'}

@app.post('/auth/login')
def login(data:LoginInput,s:Session=Depends(db)):
    expected=os.getenv('ADMIN_PASSWORD','admin-demo-password') if data.username=='admin' else os.getenv('STUDENT_PASSWORD','student-demo-password')
    u=s.query(User).filter_by(username=data.username).first()
    if not u or data.password!=expected: raise HTTPException(401,'Invalid credentials')
    token=jwt.encode({'sub':u.id,'role':u.role,'exp':datetime.now(timezone.utc)+timedelta(hours=8)},SECRET,algorithm='HS256')
    return {'access_token':token,'token_type':'bearer','role':u.role}

def job_out(j):
    return {k:getattr(j,k) for k in ['id','image_name','size_mb','processing_type','priority','latitude','longitude','required_cpu','required_memory_mb','status','worker_id','attempts','error','created_at','started_at','finished_at','result']}

@app.post('/jobs',status_code=202)
def submit(data:JobInput,idempotency_key:Optional[str]=Header(None,alias='Idempotency-Key'),u:User=Depends(auth),s:Session=Depends(db)):
    if idempotency_key and len(idempotency_key)>128: raise HTTPException(422,'Idempotency key too long')
    if idempotency_key:
        existing=s.query(Job).filter_by(owner=u.id,idempotency_key=idempotency_key).first()
        if existing: return job_out(existing)
    j=Job(id=str(uuid.uuid4()),owner=u.id,idempotency_key=idempotency_key,**data.model_dump())
    s.add(j); event(s,'JOB_SUBMITTED',j.id,data.model_dump()); s.commit()
    return job_out(j)

@app.get('/jobs')
def list_jobs(status:Optional[str]=None,limit:int=Query(100,ge=1,le=500),u:User=Depends(auth),s:Session=Depends(db)):
    q=s.query(Job)
    if u.role!='admin':q=q.filter_by(owner=u.id)
    if status:q=q.filter_by(status=status)
    return [job_out(j) for j in q.order_by(Job.created_at.desc()).limit(limit)]

@app.get('/jobs/{job_id}')
def get_job(job_id:str,u:User=Depends(auth),s:Session=Depends(db)):
    j=s.get(Job,job_id)
    if not j or (u.role!='admin' and j.owner!=u.id):raise HTTPException(404,'Job not found')
    return job_out(j)

@app.delete('/jobs/{job_id}')
def cancel(job_id:str,u:User=Depends(auth),s:Session=Depends(db)):
    j=s.get(Job,job_id)
    if not j or (u.role!='admin' and j.owner!=u.id):raise HTTPException(404,'Job not found')
    if j.status!='QUEUED':raise HTTPException(409,'Only queued jobs can be cancelled')
    j.status='CANCELLED';j.finished_at=now();event(s,'JOB_CANCELLED',j.id);s.commit()
    return {'id':j.id,'status':j.status}

@app.get('/scheduler/queue')
def queue(u:User=Depends(admin),s:Session=Depends(db)):
    jobs=s.query(Job).filter_by(status='QUEUED').all()
    return [{'id':j.id,'priority':j.priority,'score':round(score(j),2),'size_mb':j.size_mb} for j in sorted(jobs,key=score,reverse=True)]

@app.get('/workers')
def workers(u:User=Depends(admin),s:Session=Depends(db)):
    return [{'id':w.id,'cpu':w.cpu,'memory_mb':w.memory_mb,'active_job':w.active_job,'last_seen':w.last_seen} for w in s.query(Worker).all()]

@app.get('/events')
def events(limit:int=Query(100,ge=1,le=500),u:User=Depends(admin),s:Session=Depends(db)):
    return [{'kind':e.kind,'job_id':e.job_id,'payload':json.loads(e.payload),'at':e.created_at} for e in s.query(Event).order_by(Event.created_at.desc()).limit(limit)]

@app.get('/metrics')
def metrics(u:User=Depends(admin),s:Session=Depends(db)):
    jobs=s.query(Job).all()
    completed=[j for j in jobs if j.status=='COMPLETED' and j.started_at and j.finished_at]
    waits=[(j.started_at-j.created_at).total_seconds() for j in completed if j.created_at]
    durations=[(j.finished_at-j.started_at).total_seconds() for j in completed]
    per_priority={p:round(max([(j.started_at-j.created_at).total_seconds() for j in completed if j.priority==p and j.created_at] or [0]),2) for p in PRIORITY}
    all_created=[x.created_at for x in jobs if x.created_at]
    min_created=min(all_created) if all_created else None
    span=max([(j.finished_at-min_created).total_seconds() for j in completed] or [0]) if min_created else 0
    return {
        'total':len(jobs),
        'completed':len(completed),
        'queued':sum(j.status=='QUEUED' for j in jobs),
        'failed':sum(j.status=='FAILED' for j in jobs),
        'average_wait_seconds':round(sum(waits)/len(waits),2) if waits else 0,
        'average_processing_seconds':round(sum(durations)/len(durations),2) if durations else 0,
        'throughput_jobs_per_minute':round(len(completed)*60/span,2) if span else 0,
        'max_wait_seconds_by_priority':per_priority,
        'retries':sum(max(0,(j.attempts or 0)-1) for j in jobs)
    }
