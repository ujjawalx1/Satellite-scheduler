from locust import HttpUser, task, between
import random, os

def get_env_var(key, default):
    val = os.getenv(key)
    if val: return val
    if os.path.exists('.env'):
        try:
            with open('.env') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + '='):
                        return line.split('=', 1)[1].strip().strip('"\'')
        except Exception:
            pass
    return default

class SatelliteUser(HttpUser):
    wait_time = between(0.1, 1)

    def on_start(self):
        password = get_env_var('STUDENT_PASSWORD', 'student-demo-password')
        r = self.client.post('/auth/login', json={'username': 'student', 'password': password})
        self.headers = {'Authorization': 'Bearer ' + r.json()['access_token']}

    @task(4)
    def submit(self):
        self.client.post('/jobs', headers=self.headers, json={
            'image_name': 'satellite_demo.tif',
            'size_mb': random.randint(100, 2000),
            'processing_type': 'flood_mapping',
            'priority': random.choice(['LOW', 'NORMAL', 'HIGH', 'EMERGENCY']),
            'latitude': 28.61,
            'longitude': 77.21,
            'required_cpu': 2,
            'required_memory_mb': 1024
        })

    @task(1)
    def view(self):
        self.client.get('/jobs?limit=20', headers=self.headers)
