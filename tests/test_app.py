import os
import unittest
import json
import tempfile
import shutil
import importlib

# Dynamically import the module to bypass python's hyphenated folder import restriction
app_module = importlib.import_module("src.chimera-server.app")
app = app_module.app


class ChimeraServerTestCase(unittest.TestCase):
    def setUp(self):
        # Setup application in testing mode
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.app = app.test_client()

        # Create a temporary sandbox directory for test data/config
        self.test_dir = tempfile.mkdtemp()

        # Override data directories in app module
        self.old_data_dir = app_module.DATA_DIR
        self.old_config_file = app_module.CONFIG_FILE
        self.old_shared_dir = app_module.SHARED_DIR

        app_module.DATA_DIR = self.test_dir
        app_module.CONFIG_DIR = os.path.join(self.test_dir, "config")
        app_module.SHARED_DIR = os.path.join(self.test_dir, "shared")
        app_module.CONFIG_FILE = os.path.join(app_module.CONFIG_DIR, "config.json")

        os.makedirs(app_module.CONFIG_DIR, exist_ok=True)
        os.makedirs(app_module.SHARED_DIR, exist_ok=True)

        # Re-initialize path boundaries in local tests
        self.config_file = app_module.CONFIG_FILE
        self.shared_dir = app_module.SHARED_DIR

        # Force re-initialization of secret key inside the temporary config directory
        app_module.init_secret_key()

    def tearDown(self):
        # Restore old directories
        app_module.DATA_DIR = self.old_data_dir
        app_module.CONFIG_FILE = self.old_config_file
        app_module.SHARED_DIR = self.old_shared_dir

        # Clean up temporary test sandbox directory
        shutil.rmtree(self.test_dir)

    def test_first_run_redirects_to_setup(self):
        # When no config exists, accessing index should redirect to setup
        response = self.app.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/setup"))

    def test_setup_and_authorization_flow(self):
        # 1. Fetch setup page
        response = self.app.get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Initialize Server", response.data)

        # Get CSRF token from the session created during the request
        with self.app.session_transaction() as sess:
            csrf_token = sess.get("csrf_token")
            self.assertIsNotNone(csrf_token)

        # 2. Submit Setup Form with matching passwords
        response = self.app.post(
            "/setup",
            data={
                "password": "supersecureadmin",
                "confirm_password": "supersecureadmin",
                "csrf_token": csrf_token,
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Internet Gateway", response.data)

        # Verify config saved password hash
        with open(self.config_file) as f:
            cfg = json.load(f)
        self.assertIsNotNone(cfg.get("password_hash"))
        self.assertIsNotNone(cfg.get("secret_key"))

        # Log out
        self.app.get("/logout")

        # 3. Accessing index now redirects to login
        response = self.app.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))

        # 4. Attempt login with wrong password
        with self.app.session_transaction() as sess:
            csrf_token = sess.get("csrf_token")

        response = self.app.post(
            "/login", data={"password": "wrongpassword", "csrf_token": csrf_token}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid administrator password", response.data)

        # 5. Attempt login with correct password
        response = self.app.post(
            "/login",
            data={"password": "supersecureadmin", "csrf_token": csrf_token},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Internet Gateway", response.data)

    def test_csrf_middleware_blocks_unauthorized_post(self):
        # Setup password first
        with self.app.session_transaction() as sess:
            sess["csrf_token"] = "token123"

        # Submitting without csrf_token must fail
        response = self.app.post(
            "/setup",
            data={"password": "password123", "confirm_password": "password123"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Security Check Failed", response.data)

        # Submitting with invalid csrf_token must fail
        response = self.app.post(
            "/setup",
            data={
                "password": "password123",
                "confirm_password": "password123",
                "csrf_token": "wrongtoken",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_file_management_security_limits(self):
        # Initialize credentials and authenticate session
        with self.app.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf_token"] = "token123"

        # Create a dummy file in data directory
        test_file = os.path.join(self.shared_dir, "notes.txt")
        with open(test_file, "w") as f:
            f.write("This is secret server documentation.")

        # 1. Browse files
        response = self.app.get("/files")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"notes.txt", response.data)

        # 2. Download file
        response = self.app.get("/download/notes.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"This is secret server documentation.")

        # 3. Path Traversal blocker check
        response = self.app.get("/download/../config/config.json")
        self.assertEqual(response.status_code, 404)

        # 4. Delete file
        response = self.app.post(
            "/delete/notes.txt", data={"csrf_token": "token123"}, follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"notes.txt", response.data)
        self.assertFalse(os.path.exists(test_file))

    def test_api_status_payload_structure(self):
        with self.app.session_transaction() as sess:
            sess["authenticated"] = True

        response = self.app.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)

        # Verify JSON properties exist
        self.assertIn("tunnel_status", data)
        self.assertIn("system_info", data)
        self.assertIn("local_ip", data)
        self.assertIn("uptime", data)
        self.assertIn("services", data)

        # Verify CPU, RAM, Disk structure
        self.assertIn("cpu", data["system_info"])
        self.assertIn("ram", data["system_info"])
        self.assertIn("disk", data["system_info"])
        self.assertIn("network", data["system_info"])

    def test_settings_route(self):
        # 1. Unauthenticated request without CSRF should fail with 400 (CSRF Blocked)
        response = self.app.post(
            "/settings",
            data={"discord_webhook": "https://discord.com/api/webhooks/123/456"},
        )
        self.assertEqual(response.status_code, 400)

        # 2. Unauthenticated request with valid CSRF should fail with 401 (Unauthorized)
        with self.app.session_transaction() as sess:
            sess["csrf_token"] = "token123"
        response = self.app.post(
            "/settings",
            data={
                "discord_webhook": "https://discord.com/api/webhooks/123/456",
                "csrf_token": "token123",
            },
        )
        self.assertEqual(response.status_code, 401)

        # Authenticate session
        with self.app.session_transaction() as sess:
            sess["authenticated"] = True
            sess["csrf_token"] = "token123"

        # 3. Authenticated but missing CSRF token should fail with 400
        response = self.app.post(
            "/settings",
            data={"discord_webhook": "https://discord.com/api/webhooks/123/456"},
        )
        self.assertEqual(response.status_code, 400)

        # 3. Invalid webhook domain URL should fail
        response = self.app.post(
            "/settings",
            data={
                "discord_webhook": "https://evil.com/webhooks/123",
                "csrf_token": "token123",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Validation Error", response.data)

        # 4. Valid Discord Webhook URL should pass and save to config
        response = self.app.post(
            "/settings",
            data={
                "discord_webhook": "https://discord.com/api/webhooks/999/888",
                "csrf_token": "token123",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Verify saved in config
        with open(self.config_file) as f:
            cfg = json.load(f)
        self.assertEqual(
            cfg.get("discord_webhook"), "https://discord.com/api/webhooks/999/888"
        )

        # 5. Empty settings should clear the webhook
        response = self.app.post(
            "/settings", data={"discord_webhook": "", "csrf_token": "token123"}
        )
        self.assertEqual(response.status_code, 302)
        with open(self.config_file) as f:
            cfg = json.load(f)
        self.assertIsNone(cfg.get("discord_webhook"))


if __name__ == "__main__":
    unittest.main()
