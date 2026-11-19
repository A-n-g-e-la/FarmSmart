"""Optional real Edge smoke test; no packages installed, no development DB used.

Run from project/: .venv/Scripts/python.exe -B tests/browser_frontend_smoke.py
Uses a temporary browser profile and an isolated in-memory test database only.
"""

import html
import json
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dotenv
import mysql.connector
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server, WSGIRequestHandler


class QuietHandler(WSGIRequestHandler):
    def log_request(self,*args,**kwargs):
        pass


def main():
    edge=Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.is_file():
        raise SystemExit("Edge is not installed; smoke test not run")
    with patch.object(dotenv,"load_dotenv",return_value=False), patch.object(
            mysql.connector,"connect",side_effect=AssertionError("Live MySQL forbidden")) as database:
        from tests.test_officer_plan_access import OfficerPlanAccessTests
        from app import create_app
        from app.services import registration_service,officer_registration_service,authentication_service
        fixture=OfficerPlanAccessTests()
        original_connect=sqlite3.connect
        with patch.object(sqlite3,"connect",side_effect=lambda *a,**kw:original_connect(*a,**{**kw,"check_same_thread":False})):
            fixture.setUp()
        fixture.db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        fixture.db.execute("ALTER TABLE farmers ADD COLUMN location TEXT")
        fixture.db.execute("ALTER TABLE agricultural_officers ADD COLUMN region_assigned TEXT")
        fixture.db.execute("ALTER TABLE agricultural_officers ADD COLUMN organization TEXT")
        password=generate_password_hash("Private-demo-password-42",method="scrypt")
        fixture.db.execute("UPDATE users SET password_hash=? WHERE user_id IN (1,41)",(password,))
        for module in (registration_service,officer_registration_service,authentication_service):
            mock=patch.object(module,"get_db_connection",return_value=fixture.connection)
            mock.start();fixture.addCleanup(mock.stop)
        app=create_app();app.config.update(TESTING=True,SECRET_KEY="isolated-real-browser-smoke")
        driver=(Path(__file__).parent/"frontend/browser-driver.js").read_text(encoding="utf-8")
        def application(environ,start_response):
            if environ.get("PATH_INFO")=="/_frontend_test_driver":
                body=('<!doctype html><meta charset="utf-8"><pre id="report">running</pre>'
                      '<iframe id="web" style="width:1200px;height:1000px"></iframe><script>'+driver+'</script>').encode()
                start_response("200 OK",[("Content-Type","text/html; charset=utf-8"),("Cache-Control","no-store")])
                return [body]
            return app(environ,start_response)
        server=make_server("127.0.0.1",0,application,request_handler=QuietHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        profile=tempfile.mkdtemp(prefix="farmadvis-edge-smoke-")
        try:
            result=subprocess.run([str(edge),"--headless=new","--disable-gpu","--no-first-run",
                "--no-default-browser-check","--disable-extensions","--user-data-dir="+profile,
                "--dump-dom","--virtual-time-budget=90000",f"http://127.0.0.1:{server.server_port}/_frontend_test_driver"],
                capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=150,
                creationflags=subprocess.CREATE_NO_WINDOW)
            match=re.search(r'<pre id="report"[^>]*data-result="([^"]+)"[^>]*>(.*?)</pre>',result.stdout,re.S)
            if not match:
                print("Browser did not complete the driver; return code",result.returncode)
                print(result.stdout[-1200:]);raise SystemExit(1)
            summary=json.loads(html.unescape(match.group(2)))
            print(json.dumps(summary,indent=2))
            print("Temporary browser profile:",profile)
            database.assert_not_called()
            if match.group(1)!="passed":raise SystemExit(1)
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close();fixture.doCleanups()


if __name__=="__main__":
    main()
