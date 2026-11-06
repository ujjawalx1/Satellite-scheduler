import pytest, os
from fastapi.testclient import TestClient
from app.core import app, SessionLocal, User, Job, Event, Worker, Base, engine

@pytest.fixture(scope="session", autouse=True)
def setup_database():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        for name, role in [('student', 'user'), ('admin', 'admin')]:
            if not s.query(User).filter_by(username=name).first():
                s.add(User(id=name, username=name, role=role))
        s.commit()
    yield

@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c

@pytest.fixture(scope="session")
def student_token(client):
    pwd = os.getenv('STUDENT_PASSWORD', 'student-demo-password')
    r = client.post('/auth/login', json={'username': 'student', 'password': pwd})
    if r.status_code != 200:
        r = client.post('/auth/login', json={'username': 'student', 'password': 'student-demo-password'})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()['access_token']

@pytest.fixture(scope="session")
def admin_token(client):
    pwd = os.getenv('ADMIN_PASSWORD', 'admin-demo-password')
    r = client.post('/auth/login', json={'username': 'admin', 'password': pwd})
    if r.status_code != 200:
        r = client.post('/auth/login', json={'username': 'admin', 'password': 'admin-demo-password'})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()['access_token']

def test_health(client):
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json() == {'status': 'ok', 'service': 'satellite-scheduler'}

def test_login_invalid(client):
    r = client.post('/auth/login', json={'username': 'student', 'password': 'wrong-nonexistent-password'})
    assert r.status_code == 401

def test_job_submission_and_retrieval(client, student_token):
    headers = {'Authorization': f'Bearer {student_token}'}
    payload = {
        'image_name': 'test_satellite.tif',
        'size_mb': 500,
        'processing_type': 'flood_mapping',
        'priority': 'NORMAL',
        'latitude': 28.61,
        'longitude': 77.21,
        'required_cpu': 2,
        'required_memory_mb': 1024
    }
    r = client.post('/jobs', headers=headers, json=payload)
    assert r.status_code == 202
    job_data = r.json()
    assert job_data['status'] == 'QUEUED'
    job_id = job_data['id']

    r = client.get(f'/jobs/{job_id}', headers=headers)
    assert r.status_code == 200
    assert r.json()['id'] == job_id

def test_idempotency(client, student_token):
    headers = {'Authorization': f'Bearer {student_token}', 'Idempotency-Key': 'unique-key-12345'}
    payload = {
        'image_name': 'test_idem.tif',
        'size_mb': 250,
        'processing_type': 'cloud_detection',
        'priority': 'HIGH',
        'latitude': 12.34,
        'longitude': 56.78,
        'required_cpu': 1,
        'required_memory_mb': 512
    }
    r1 = client.post('/jobs', headers=headers, json=payload)
    assert r1.status_code == 202
    job1_id = r1.json()['id']

    r2 = client.post('/jobs', headers=headers, json=payload)
    assert r2.status_code == 202
    assert r2.json()['id'] == job1_id

def test_job_cancellation(client, student_token):
    headers = {'Authorization': f'Bearer {student_token}'}
    payload = {
        'image_name': 'test_cancel.tif',
        'size_mb': 300,
        'processing_type': 'thumbnail',
        'priority': 'LOW',
        'latitude': 0.0,
        'longitude': 0.0,
        'required_cpu': 1,
        'required_memory_mb': 512
    }
    r = client.post('/jobs', headers=headers, json=payload)
    job_id = r.json()['id']

    cancel_r = client.delete(f'/jobs/{job_id}', headers=headers)
    assert cancel_r.status_code == 200
    assert cancel_r.json()['status'] == 'CANCELLED'

def test_admin_metrics_and_queue(client, admin_token):
    headers = {'Authorization': f'Bearer {admin_token}'}
    r_queue = client.get('/scheduler/queue', headers=headers)
    assert r_queue.status_code == 200
    assert isinstance(r_queue.json(), list)

    r_metrics = client.get('/metrics', headers=headers)
    assert r_metrics.status_code == 200
    assert 'total' in r_metrics.json()
    assert 'throughput_jobs_per_minute' in r_metrics.json()

def test_forbidden_for_non_admin(client, student_token):
    headers = {'Authorization': f'Bearer {student_token}'}
    r = client.get('/scheduler/queue', headers=headers)
    assert r.status_code == 403
    r = client.get('/metrics', headers=headers)
    assert r.status_code == 403
