
import http.server, socketserver, os, threading, sys, time

class SilentHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

httpd = socketserver.TCPServer(("127.0.0.1", 9998), SilentHandler)
t = threading.Thread(target=httpd.serve_forever, daemon=True)
t.start()
pid = os.getpid()
print(f"BG_SERVER_PID={pid}", flush=True)
# Write PID to a file so we can find it
open(r"C:\Users\Alex R\Projects\webagent\bg_pid.txt", "w").write(str(pid))
sys.stdout.flush()
time.sleep(600)  # stay alive 10 min
