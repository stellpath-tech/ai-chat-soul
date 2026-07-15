import unittest

import web

from channel.web.metrics import metrics_processor


class RedirectHandler:
    def GET(self):
        raise web.seeother("/chat")


class MetricsProcessorTest(unittest.TestCase):
    def setUp(self):
        self.old_debug = web.config.debug
        web.config.debug = False
        self.app = web.application(("/", "RedirectHandler"), globals())
        self.app.add_processor(metrics_processor)

    def tearDown(self):
        web.config.debug = self.old_debug

    def test_redirect_keeps_http_status(self):
        response = self.app.request("/")

        self.assertEqual("303 See Other", response.status)
        self.assertTrue(response.headers["Location"].endswith("/chat"))

    def test_unknown_route_returns_404_without_traceback(self):
        response = self.app.request("/missing")

        self.assertEqual("404 Not Found", response.status)
        self.assertNotIn(b"Traceback", response.data)
        self.assertNotIn(b"AttributeError", response.data)


if __name__ == "__main__":
    unittest.main()
