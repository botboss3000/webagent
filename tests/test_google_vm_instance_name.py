import unittest

from app.deploy.providers.google_vm import GoogleVMProvider


class GoogleVMInstanceNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_custom_name_fails_before_cloud_authentication(self):
        provider = GoogleVMProvider()
        events = []
        async for event in provider.deploy(
            {"project_id": "example-project", "instance_name": "Not Valid!"},
            {},
        ):
            events.append(event)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["phase"], "done")
        self.assertFalse(events[0]["result"]["ok"])
        self.assertIn("Instance name", events[0]["result"]["message"])


if __name__ == "__main__":
    unittest.main()
