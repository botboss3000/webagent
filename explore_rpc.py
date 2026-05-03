import json
import os
from dotenv import load_dotenv
load_dotenv()

url = os.environ.get('SUPABASE_URL')
key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
print(f"URL: {url}")
print(f"Key present: {bool(key)}")

from supabase import create_client
client = create_client(url, key)

# Try to get the OpenAPI spec via the root endpoint
import httpx
spec_url = f"{url}/rest/v1/"
headers = {
    "apikey": key,
    "Authorization": f"Bearer {key}"
}
resp = httpx.get(spec_url, headers=headers)
if resp.status_code == 200:
    spec = resp.json()
    # Find all rpc endpoints
    rpc_endpoints = []
    for path, methods in spec.get('paths', {}).items():
        if '/rpc/' in path:
            rpc_endpoints.append(path)
    print("\nFound RPC endpoints:", len(rpc_endpoints))
    for ep in rpc_endpoints[:20]:
        print(f"  {ep}")
else:
    print(f"Failed to fetch spec: {resp.status_code}")
    print(resp.text[:200])

# Try to call a known function 'rls_auto_enable'
try:
    result = client.rpc('rls_auto_enable', {}).execute()
    print("\nrls_auto_enable result:", result.data)
except Exception as e:
    print("\nError calling rls_auto_enable:", e)

# Try to list all functions from information_schema.routines via REST
try:
    resp = client.postgrest.from_('information_schema.routines').select('routine_name, routine_schema').limit(20).execute()
    print("\nRoutines:", resp.data)
except Exception as e:
    print("\nError getting routines:", e)

# Try to query pg_catalog.pg_proc (might not be exposed)
try:
    resp = client.postgrest.from_('pg_catalog.pg_proc').select('proname').limit(10).execute()
    print("\npg_proc:", resp.data)
except Exception as e:
    print("\nError getting pg_proc:", e)