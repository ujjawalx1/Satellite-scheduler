import argparse, random, time, concurrent.futures, requests, os, sys

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

p = argparse.ArgumentParser()
p.add_argument('--count', type=int, default=100)
p.add_argument('--concurrency', type=int, default=10)
p.add_argument('--url', default='http://localhost:8000')
args = p.parse_args()

password = get_env_var('STUDENT_PASSWORD', 'student-demo-password')
try:
    resp = requests.post(args.url + '/auth/login', json={'username': 'student', 'password': password}, timeout=10)
    resp.raise_for_status()
    token = resp.json()['access_token']
except Exception as e:
    print(f"Failed to authenticate with {args.url}: {e}")
    sys.exit(1)

headers = {'Authorization': 'Bearer ' + token}
random.seed(66)

def submit(i):
    try:
        r = requests.post(args.url + '/jobs', headers={**headers, 'Idempotency-Key': f'load-{int(time.time())}-{i}'}, json={
            'image_name': f'satellite_{i:05d}.tif', 'size_mb': random.randint(100, 2000),
            'processing_type': random.choice(['flood_mapping', 'vegetation_index', 'cloud_detection', 'thumbnail']),
            'priority': random.choices(['LOW', 'NORMAL', 'HIGH', 'EMERGENCY'], weights=[20, 50, 25, 5])[0],
            'latitude': round(random.uniform(-80, 80), 4), 'longitude': round(random.uniform(-170, 170), 4),
            'required_cpu': random.choice([1, 2, 4, 8]), 'required_memory_mb': random.choice([512, 1024, 2048, 4096])
        }, timeout=20)
        return r.status_code
    except Exception:
        return 0

start = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
    codes = list(pool.map(submit, range(args.count)))
print({'submitted': len(codes), 'accepted': codes.count(202), 'errors': len(codes) - codes.count(202), 'seconds': round(time.monotonic() - start, 2)})
