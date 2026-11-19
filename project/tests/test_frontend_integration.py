"""Rendered frontend routes plus the existing isolated persistence/API workflow."""

import re
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_farmer_plot_integration import PlotTestBase
from app import create_app


class FrontendIntegrationTests(PlotTestBase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.app.config.update(TESTING=True,SECRET_KEY="isolated-frontend-tests")
        self.client = self.app.test_client()
        for name,identity in (("farmer",{"user_id":41,"farmer_id":91}),
                              ("officer",{"user_id":42,"agricultural_officer_id":7})):
            module = "authentication_service" if name == "farmer" else "officer_authentication_service"
            lookup = patch(f"app.security.get_active_{name}_identity",return_value={"ok":True,"identity":identity})
            lookup.start();self.addCleanup(lookup.stop)
            auth = patch(f"app.routes.{name}.authenticate_{name}",return_value={"ok":True,"identity":identity})
            auth.start();self.addCleanup(auth.stop)

    def sign_in(self,role="farmer"):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=41 if role=="farmer" else 42,authenticated_at=time.time())
            session["farmer_id" if role=="farmer" else "agricultural_officer_id"] = 91 if role=="farmer" else 7

    def test_public_landing_account_choices_and_recovered_pages(self):
        response=self.client.get("/")
        self.assertEqual(response.status_code,200)
        for url in ("/farmer/login-page","/farmer/registration","/officer/login-page","/officer/register-page"):
            self.assertIn(url,response.get_data(as_text=True))
            page=self.client.get(url)
            self.assertEqual(page.status_code,200,page.get_data(as_text=True))
            for asset in re.findall(r'(?:src|href)="(/static/[^\"]+)"',page.get_data(as_text=True)):
                self.assertEqual(self.client.get(asset).status_code,200,asset)

    def test_farmer_html_and_json_contracts_coexist_without_ussd(self):
        self.sign_in()
        html=self.client.get("/farmer/home",headers={"Accept":"text/html"})
        self.assertEqual(html.status_code,200)
        text=html.get_data(as_text=True)
        self.assertIn('id="agricultural-plan-form"',text)
        self.assertIn('id="plot-area-unit"',text)
        self.assertNotIn('id="ussd-pin"',text)
        self.assertTrue(self.client.get("/farmer/home").is_json)
        browser_accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        self.assertIn('id="agricultural-plan-form"',self.client.get("/farmer/home",headers={"Accept":browser_accept}).get_data(as_text=True))
        self.assertEqual(html.headers["Cache-Control"],"no-store")
        ids=set(re.findall(r'\bid="([^\"]+)"',text))
        source=(Path(__file__).resolve().parents[1]/"app/static/javascript/agricultural-plans.js").read_text(encoding="utf-8")
        required=set(re.findall(r'(?:el|document.getElementById)\("([^\"]+)"\)',source))
        self.assertFalse(required-ids,required-ids)

    def test_officer_dashboard_uses_recovered_html_and_read_only_plan_panel(self):
        self.sign_in("officer")
        response=self.client.get("/officer/dashboard",headers={"Accept":"text/html"})
        self.assertEqual(response.status_code,200)
        text=response.get_data(as_text=True)
        self.assertIn('id="assigned-farmers"',text)
        self.assertIn('id="officer-plans"',text)
        self.assertNotIn('id="agricultural-plan-form"',text)
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_new_plan_comparison_and_choice_http_journey(self):
        self.sign_in()
        headers={"X-CSRF-Token":self.client.get("/farmer/agricultural-plans/csrf").json["csrf_token"]}
        response=self.client.post("/farmer/plots/11/agricultural-plans",json=self.data,headers=headers)
        self.assertEqual(response.status_code,201,response.json)
        plan_id=response.json["result"]["plan_id"]
        url=f"/farmer/agricultural-plans/{plan_id}"
        rows=self.client.get(url+"/comparisons").json["result"]["scenarios"]
        self.assertEqual([r["scaling_percentage"] for r in rows],[0,75,100,125,150])
        self.assertIsNone(self.client.get(url).json["result"]["selection"])
        result=self.client.put(url+"/selection",json={"scenario_id":"reduced"},headers=headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(self.client.get(url).json["result"]["selection"]["scenario_id"],"reduced")
        self.assertEqual(self.client.get(url).json["result"]["fertilizers"][0]["quantity_kg"],50)

    def test_unavailable_scenario_cannot_be_selected(self):
        self.sign_in()
        headers={"X-CSRF-Token":self.client.get("/farmer/agricultural-plans/csrf").json["csrf_token"]}
        data={**self.data,"fertilizers":[{"product":"UREA","quantity_kg":250}]}
        created=self.client.post("/farmer/plots/11/agricultural-plans",json=data,headers=headers)
        url=f"/farmer/agricultural-plans/{created.json['result']['plan_id']}"
        rows=self.client.get(url+"/comparisons").json["result"]["scenarios"]
        self.assertEqual(rows[-1]["status"],"unavailable")
        self.assertEqual(self.client.put(url+"/selection",json={"scenario_id":"higher"},headers=headers).status_code,400)

    def test_login_and_logout_preserve_existing_security_contract(self):
        token=self.client.get("/farmer/login").json["csrf_token"]
        response=self.client.post("/farmer/login",json={"phone_number":"0712345678","password":"synthetic-private-password","csrf_token":token})
        self.assertEqual(response.status_code,200)
        logout=response.json["csrf_token"]
        self.assertEqual(self.client.get("/farmer/home",headers={"Accept":"text/html"}).status_code,200)
        self.assertEqual(self.client.post("/farmer/logout",headers={"X-CSRF-Token":logout}).status_code,200)
        self.assertEqual(self.client.get("/farmer/home").status_code,401)
