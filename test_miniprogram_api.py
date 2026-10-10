import asyncio
import unittest

from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.api.miniprogram_v1 import (
    MINIPROGRAM_API_PREFIX,
    MiniprogramApiStatus,
    get_miniprogram_api_status,
)
from app.database import engine
from app.main import app, enforce_active_user_session


STATUS_PATH = f"{MINIPROGRAM_API_PREFIX}/status"


def build_request(
    path: str,
    cookie: bytes | None = None,
) -> Request:
    headers = []
    if cookie is not None:
        headers.append((b"cookie", cookie))

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
    )


class MiniprogramApiTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def test_status_response_has_stable_contract(self):
        response = get_miniprogram_api_status()

        self.assertIsInstance(
            response,
            MiniprogramApiStatus,
        )
        self.assertEqual(
            response.model_dump(),
            {
                "status": "ok",
                "api_version": "v1",
                "service": "mall-miniprogram-api",
            },
        )

    def test_application_registers_public_catalog_routes(self):
        routes = [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and route.path.startswith(
                MINIPROGRAM_API_PREFIX
            )
        ]

        self.assertEqual(len(routes), 4)
        self.assertEqual({route.path for route in routes}, {
            STATUS_PATH, f"{MINIPROGRAM_API_PREFIX}/categories",
            f"{MINIPROGRAM_API_PREFIX}/products",
            f"{MINIPROGRAM_API_PREFIX}/products/{{product_public_id}}",
        })
        self.assertTrue(all(route.methods == {"GET"} for route in routes))
        self.assertEqual(
            routes[0].operation_id,
            "get_miniprogram_api_status",
        )

    def test_status_route_is_present_in_openapi_schema(self):
        operation = app.openapi()["paths"][STATUS_PATH][
            "get"
        ]

        self.assertEqual(
            operation["operationId"],
            "get_miniprogram_api_status",
        )
        self.assertEqual(
            operation["tags"],
            ["miniprogram-v1"],
        )
        self.assertIn(
            "application/json",
            operation["responses"]["200"]["content"],
        )

    def test_product_detail_contract_is_read_only(self):
        operation = app.openapi()["paths"][
            f"{MINIPROGRAM_API_PREFIX}/products/{{product_public_id}}"
        ]["get"]
        self.assertEqual(operation["operationId"], "get_miniprogram_product_detail")
        self.assertIn("200", operation["responses"])
        self.assertIn("422", operation["responses"])

    def test_product_list_search_query_contract(self):
        operation = app.openapi()["paths"][
            f"{MINIPROGRAM_API_PREFIX}/products"
        ]["get"]
        query = next(param for param in operation["parameters"]
                     if param["name"] == "q")
        self.assertFalse(query["required"])
        schema = next(option for option in query["schema"]["anyOf"]
                      if option["type"] == "string")
        self.assertEqual(schema["maxLength"], 80)
        self.assertEqual(schema["pattern"], r"\S")

    def test_status_route_ignores_legacy_admin_cookie(self):
        request = build_request(
            STATUS_PATH,
            cookie=b"user_id=invalid",
        )

        async def call_next(received_request):
            self.assertIs(received_request, request)
            return JSONResponse({"reached": True})

        response = asyncio.run(
            enforce_active_user_session(
                request,
                call_next,
            )
        )

        self.assertEqual(response.status_code, 200)

    def test_existing_health_route_remains_registered(self):
        paths = {
            route.path
            for route in app.routes
            if isinstance(route, APIRoute)
        }

        self.assertIn("/health", paths)
        self.assertIn(STATUS_PATH, paths)


if __name__ == "__main__":
    unittest.main()
