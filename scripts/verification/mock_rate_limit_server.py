import http.server
import sys

class MyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Print to stdout/stderr so we can see it in logs
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt%args))

    def do_POST(self):
        print(f"Mock server: received POST request to {self.path}", flush=True)
        self.send_response(429)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"Rate limit exceeded","type":"rate_limit_error","param":null,"code":null}}')

def main():
    port = 9999
    print(f"Starting mock 429 rate limit server on 127.0.0.1:{port}...", flush=True)
    server = http.server.HTTPServer(('127.0.0.1', port), MyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Stopping mock server...", flush=True)

if __name__ == "__main__":
    main()
