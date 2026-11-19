"""Assignment-scoped officer reads using an isolated database and real auth SQL."""

import time
from unittest.mock import patch

from tests.test_farmer_plot_integration import PlotTestBase
from app import create_app
from app.services import officer_farmer_service as officers
from app.services import officer_authentication_service as auth
from app.services import agricultural_plan_service as plans


class OfficerPlanAccessTests(PlotTestBase):
    def setUp(self):
        super().setUp()
        self.db.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
            phone_number TEXT, email TEXT, role TEXT, status TEXT);
        CREATE TABLE agricultural_officers (agricultural_officer_id INTEGER PRIMARY KEY,
            user_id INTEGER, approved_at TEXT);
        ALTER TABLE farmers ADD COLUMN user_id INTEGER;
        ALTER TABLE farmers ADD COLUMN assigned_officer_id INTEGER;
        INSERT INTO users VALUES (1,'Farmer','One','+254712345678',NULL,'farmer','active'),
          (2,'Farmer','Two','+254712345679',NULL,'farmer','active'),
          (3,'Unassigned','Farmer',NULL,NULL,'farmer','active'),
          (4,'Disabled','Farmer',NULL,NULL,'farmer','disabled'),
          (5,'No plots','Farmer',NULL,NULL,'farmer','active'),
          (41,'Officer','One',NULL,'one@example.test','officer','active'),
          (42,'Officer','Two',NULL,'two@example.test','officer','active'),
          (43,'Officer','Pending',NULL,'pending@example.test','officer','pending');
        INSERT INTO agricultural_officers VALUES (7,41,'2026-01-01'),(8,42,'2026-01-01'),(9,43,NULL);
        UPDATE farmers SET user_id=1,assigned_officer_id=7 WHERE farmer_id=91;
        UPDATE farmers SET user_id=2,assigned_officer_id=8 WHERE farmer_id=92;
        INSERT INTO farmers VALUES (93,3,NULL),(94,4,7),(95,5,7);
        INSERT INTO farm_plots (plot_id,farmer_id,area_ha) VALUES (31,93,1);
        """)
        for module in (officers, auth):
            connect = patch.object(module, "get_db_connection", return_value=self.connection)
            connect.start()
            self.addCleanup(connect.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-plan-tests")
        self.client = self.app.test_client()

    def sign_in(self, user=41, officer=7):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=user, agricultural_officer_id=officer, authenticated_at=time.time())

    def test_dashboard_and_farmer_reads_are_assignment_scoped(self):
        self.sign_in()
        result = self.client.get("/officer/dashboard")
        self.assertEqual(result.status_code, 200)
        self.assertEqual([f["farmer_id"] for f in result.json["farmers"]], [91,95])
        self.assertEqual(result.json["farmers"][0]["plot_count"], 2)
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/officer/api/farmers/92/plots").status_code, 404)
        self.assertEqual(self.client.get("/officer/api/farmers/93/plots").status_code, 404)
        self.sign_in(42,8)
        self.assertEqual([f["farmer_id"] for f in self.client.get("/officer/api/farmers").json["farmers"]], [92])

    def test_officer_reads_canonical_plot_area_and_existing_location(self):
        plot_id = self.register()
        self.sign_in()
        result = self.client.get("/officer/api/farmers/91/plots")
        self.assertEqual(result.status_code, 200)
        plot = next(p for p in result.json["plots"] if p["plot_id"] == plot_id)
        self.assertEqual(plot["area_ha"], "0.40468564224")
        self.assertEqual(plot["area_acres"], "1")
        self.assertEqual(plot["county"], "Uasin Gishu")

    def test_plans_include_products_and_separate_selection_without_mutation(self):
        first = self.create()
        second = self.create(planting_season="short_rains")
        plans.select_scenario(91,first["plan_id"],"reduced")
        before = plans.get_plan(91,first["plan_id"])
        self.sign_in()
        response = self.client.get("/officer/api/farmers/91/plans")
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual({p["plan_id"] for p in response.json["plans"]}, {first["plan_id"],second["plan_id"]})
        detail = self.client.get(f"/officer/api/farmers/91/plans/{first['plan_id']}").json["plan"]
        self.assertEqual(detail,before)
        self.assertEqual(detail["selection"]["scenario_id"],"reduced")
        self.assertEqual(plans.get_plan(91,first["plan_id"]),before)
        for word in ("password", "password_hash", "email", "assigned_officer_id"):
            self.assertNotIn(word, str(response.json))

    def test_plot_plan_filter_and_empty_lists_are_distinguished_from_denial(self):
        plan = self.create()
        self.create(plot=12)
        self.sign_in()
        rows = self.client.get("/officer/api/farmers/91/plots/11/plans").json["plans"]
        self.assertEqual([r["plan_id"] for r in rows], [plan["plan_id"]])
        self.assertEqual(self.client.get("/officer/api/farmers/95/plans").json["plans"], [])
        self.assertEqual(self.client.get("/officer/api/farmers/95/plots").json["plots"], [])
        self.assertEqual(self.client.get("/officer/api/farmers/91/plots/21/plans").status_code,404)

    def test_other_farmers_plans_are_hidden_even_with_forged_ids(self):
        foreign = self.create(plot=21,farmer=92)
        self.sign_in()
        for url in ("/officer/api/farmers/92/plans", "/officer/api/farmers/93/plans",
                    f"/officer/api/farmers/91/plans/{foreign['plan_id']}",
                    f"/officer/api/farmers/92/plans/{foreign['plan_id']}"):
            response = self.client.get(url+"?agricultural_officer_id=8&farmer_id=92",headers={"X-Officer-Id":"8"})
            self.assertEqual(response.status_code,404)

    def test_plan_endpoints_do_not_allow_mutating_methods(self):
        plan=self.create()
        self.sign_in()
        for url in ("/officer/api/farmers/91/plans", "/officer/api/farmers/91/plots/11/plans",
                    f"/officer/api/farmers/91/plans/{plan['plan_id']}"):
            for method in ("POST","PUT","PATCH","DELETE"):
                with self.subTest(url=url,method=method):
                    self.assertEqual(self.client.open(url,method=method,json={"variety":"HAC"}).status_code,405)
        self.assertEqual(plans.get_plan(91,plan["plan_id"]),plan)

    def test_officer_cannot_use_farmer_write_or_selection_endpoints(self):
        plan=self.create()
        for url,method,payload in (("/farmer/plots","POST",self.plot_data),
            ("/farmer/plots/11","PATCH",{"area_ha":2}),
            (f"/farmer/agricultural-plans/{plan['plan_id']}","PATCH",{"variety":"HAC"}),
            (f"/farmer/agricultural-plans/{plan['plan_id']}/selection","PUT",{"scenario_id":"higher"})):
            self.sign_in()
            self.assertEqual(self.client.open(url,method=method,json=payload).status_code,401)

    def test_approval_disabled_status_and_assignment_are_rechecked(self):
        plan=self.create()
        self.sign_in()
        self.db.execute("UPDATE agricultural_officers SET approved_at=NULL WHERE agricultural_officer_id=7")
        self.assertEqual(self.client.get("/officer/api/farmers/91/plans").status_code,401)
        self.db.execute("UPDATE agricultural_officers SET approved_at='2026-01-01' WHERE agricultural_officer_id=7")
        self.db.execute("UPDATE users SET status='disabled' WHERE user_id=41")
        self.sign_in()
        self.assertEqual(self.client.get("/officer/api/farmers/91/plans").status_code,401)
        self.db.execute("UPDATE users SET status='active' WHERE user_id=41")
        self.db.execute("UPDATE farmers SET assigned_officer_id=8 WHERE farmer_id=91")
        self.sign_in()
        self.assertEqual(self.client.get(f"/officer/api/farmers/91/plans/{plan['plan_id']}").status_code,404)

    def test_pending_anonymous_and_farmer_sessions_are_denied(self):
        for user,officer in ((43,9),(41,8)):
            self.sign_in(user,officer)
            self.assertEqual(self.client.get("/officer/api/farmers/91/plans").status_code,401)
        with self.client.session_transaction() as session:
            session.clear();session.update(user_id=1,farmer_id=91,authenticated_at=time.time())
        self.assertEqual(self.client.get("/officer/api/farmers/91/plans").status_code,401)
        self.assertEqual(self.client.get("/officer/api/farmers/91/plans").status_code,401)

    def test_plan_service_reads_never_commit_or_start_write_transactions(self):
        from unittest.mock import MagicMock
        plan=self.create()
        tracking=MagicMock(wraps=self.connection)
        with patch.object(officers,"get_db_connection",return_value=tracking):
            result=officers.get_assigned_farmer_plans(7,91,plan_id=plan["plan_id"])
        self.assertTrue(result["ok"],result)
        tracking.commit.assert_not_called()
        tracking.start_transaction.assert_not_called()

    def test_database_errors_are_safe(self):
        from mysql.connector import Error
        self.sign_in()
        with patch.object(officers,"get_db_connection",side_effect=Error("private SQL diagnostic")):
            response=self.client.get("/officer/api/farmers/91/plans")
        self.assertEqual(response.status_code,503)
        self.assertNotIn("private",response.get_data(as_text=True))
