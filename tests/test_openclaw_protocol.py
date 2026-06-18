import unittest

from core.openclaw import OpenClawManager


class OpenClawProtocolTest(unittest.TestCase):
    def test_connect_params_support_current_protocol(self):
        client_meta = {
            "id": "gateway-client",
            "displayName": "Open-Xiaoai Bridge",
            "version": "1.0.0",
            "platform": "python",
            "mode": "backend",
            "instanceId": "xiaoai-test",
        }
        scopes = ["operator.read", "operator.write"]

        params = OpenClawManager._build_connect_params(
            client_meta=client_meta,
            scopes=scopes,
            token="token",
        )

        self.assertEqual(3, params["minProtocol"])
        self.assertEqual(4, params["maxProtocol"])
        self.assertEqual(client_meta, params["client"])
        self.assertEqual(scopes, params["scopes"])
        self.assertEqual({"token": "token"}, params["auth"])
        self.assertNotIn("device", params)

    def test_connect_params_include_device_payload(self):
        device_payload = {
            "id": "device",
            "publicKey": "public",
            "signature": "signature",
            "signedAt": 1,
            "nonce": "nonce",
        }

        params = OpenClawManager._build_connect_params(
            client_meta={"id": "gateway-client"},
            scopes=[],
            device_payload=device_payload,
            token="token",
        )

        self.assertEqual(device_payload, params["device"])


if __name__ == "__main__":
    unittest.main()
