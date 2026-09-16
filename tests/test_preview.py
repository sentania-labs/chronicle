import http.client
import http.server
import threading

from chronicle.preview.main import make_handler


def test_serves_files_and_refuses_directory_listing(tmp_path) -> None:
    (tmp_path / "index.html").write_text("hello")
    (tmp_path / "sub").mkdir()

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(tmp_path))) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/index.html")
            response = conn.getresponse()
            assert response.status == 200
            response.read()
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/sub/")
            response = conn.getresponse()
            assert response.status == 403
            response.read()
            conn.close()
        finally:
            httpd.shutdown()
        thread.join(timeout=5)
