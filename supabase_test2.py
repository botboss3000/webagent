import os
from dotenv import load_dotenv
load_dotenv()

url = os.environ.get('SUPABASE_URL')
key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')

print(f'Testing Supabase connection to: {url}')

# Try different import approaches
try:
    # Approach 1: Direct import
    from supabase import create_client
    print('create_client imported successfully')
    
    # Try without any extra arguments
    client = create_client(url, key)
    print('Client created successfully')
    
    # Test a simple query
    result = client.table('messages').select('count', count='exact').limit(1).execute()
    print(f'Query executed: count = {result.count}')
    
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()